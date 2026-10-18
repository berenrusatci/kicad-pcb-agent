---
name: paralel-yurutme
description: Bir işi paralel dala / alt ajana vermeye değer mi — bağımsızlık, yazar-ayrıklığı, kâr ve yürütme zarfı testleri. FANOUT_APPROVED / INLINE_CHEAPER / FANOUT_UNSAFE üretir. Kilit kuralının sahibi değildir; fan-out öncesi kanıtın sahibidir. Ölçülmemiş hızlanma kanıt sayılmaz.
version: 1.0.0
last-reviewed: 2026-08-11
phase: "Faz-dışı — yürütme kapısı (her fazda, fan-out kararı anında; board kilidi almaz)"
assurance_loop: [L0, L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: fan-out aday listesi (iş birimi + yazacağı yollar), schema: references/fanout_plan.schema.json, required: true}
  - {artifact: context_manifest.json (worker başına), schema: ../../tools/schemas/context_pack.schema.json, required: true}
  - {artifact: DAG konumu (bu düğüm paralel mi seri mi), schema: null, required: true}
  - {artifact: ilgili skill'lerin writes_board bayrakları, schema: null, required: true}
outputs:
  - {artifact: fanout_plan.json, schema: references/fanout_plan.schema.json}
  - {artifact: fanout_coverage (metrik), schema: ../../tools/schemas/metric_coverage.schema.json}
writes_board: false
tools: [context-pack, gate-receipt, recovery, independent-verify, benchmark]
---

# SKILL: Paralel yürütme kapısı — fan-out bir maliyet kararıdır

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Metrik adları: [[../_shared/glossary.md]] §8 ·
Yürütme zarfı (ölçülen sayılar): [[references/execution-envelope.md]]

**Bu skill'in kanonik sahibi olduğu iki kural:**

1. *Fan-out bir maliyet kararıdır* — paralelleştirme kendini kanıtlamak
   zorundadır, varsayılan değildir. Ölçülmemiş hızlanma kanıt değildir.
2. *Fan-out öncesi yazar-ayrıklığı **kanıtı*** — dalların ayrı dosya yazması
   kuralının sahibi [[../pcb-orchestrator/references/board-write-lock.md]]'dir;
   bu skill o limitin **kanıtını** üretir. Aynı ayrım
   [[../_shared/rule-conflicts.md]] §9'daki "yukarı akış limitin sahibi, aşağı
   akış kanıtın sahibidir" hükmüdür.

> **Bu yeni bir yürütücü değildir.** DAG'ın hangi düğümünün paralel olduğunu
> [[../pcb-orchestrator/references/phase-dag.md]] söyler; board'a kimin
> yazabileceğini board-write-lock söyler. Bu skill yalnız *şu iş birimi şu anda
> ayrı dala verilmeye değer mi ve verildiğinde çakışır mı* sorusunu kapatır.
> İkinci bir orkestratör sessiz otorite çatışması yaratır.

---

## 0. Ne zaman çağrılır — ve ne zaman çağrılmaz

**Çağrılır:**

- İki veya daha fazla iş birimini aynı anda başlatmayı düşündüğünde.
- Bir işi alt ajana devretmeden **önce** (bağlam bütçesi, disiplin ayrımı).
- Beklenen süresi **2 dakikayı** aşan bir script/validator koşusundan önce
  ([[references/execution-envelope.md]] §1: foreground zarfı buradadır).
- İki dalın yazacağı yol kümesi kesişiyormuş gibi göründüğünde.
- Faz 1 (kök düğümler) ve Faz 4a (salt-okuma denetimleri) açılırken — bunlar
  DAG'da zaten paralel işaretli; kapı yalnız *ayrıklığı* kanıtlar.

**Çağrılmaz — bu durumlarda cevap zaten `INLINE_CHEAPER`:**

- Tek dosya okuması, tek grep, tek soru.
- Ana ajanda **zaten yüklü** bağlam üzerinde yapılacak iş. Alt ajan taze
  bağlamla başlar ve o bağlamı yeniden toplamak zorundadır.
- Faz 3 bariyeri altındaki board-yazan adımlar. Orada karar verilmez:
  `writes_board: true` taşıyan skill'ler **asla** paralel koşmaz
  (board-write-lock §5).
- Adım N+1 adım N'in tam çıktısını istiyorsa. Zincir tek oturumda kalır.

---

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| aday iş birimi listesi | çağıran skill / orkestratör | kapı açılmaz, `NO_COVERAGE` |
| birim başına `writes[]` (yazılacak yollar) | birimin kendi sözleşmesi | o birim `excluded` + gerekçe; dala verilmez |
| birim başına `reads[]` | context manifest | bağımsızlık testi koşamaz → seri |
| `context_manifest.json` | [[../decompose-pcb-decisions/SKILL\|decompose]] §2.3 | worker açılmaz |
| DAG konumu (paralel/seri/bariyer) | [[../pcb-orchestrator/references/phase-dag.md\|phase-dag]] | seri varsayılır |
| `writes_board` bayrakları | ilgili skill frontmatter'ları | `FANOUT_UNSAFE` |

Çıktı `fanout_plan.json`
([`references/fanout_plan.schema.json`](references/fanout_plan.schema.json)):
her aday birim için karar + gerekçe, yazar-ayrıklığı kanıtı, `fanout_coverage`
ve tek bir `gate.verdict`.

`gate.verdict` kapalı sözlüktendir ([[../_shared/coverage-gate.md]] §Verdict
sözlüğü). Bu skill'in kendi üçlüsü **ayrı alandadır**:

| `domain_state` | Anlamı |
|---|---|
| `FANOUT_APPROVED` | Dört test geçti; birim ayrı dala verilebilir |
| `INLINE_CHEAPER` | Test 3 düştü; iş ana bağlamda kalır (bir hata değildir) |
| `FANOUT_UNSAFE` | Test 1, 2 veya 4 düştü; paralel koşarsa çakışır |

Bu üçlü **plan seviyesindedir**. Birim seviyesinde dördüncü bir karar vardır:
`EXCLUDED` — birim ne yazacağını bildirmemiştir, bu yüzden ayrıklığı
kanıtlanamaz. Sessizlik ayrıklık değildir: `EXCLUDED` birim dala verilmez,
`scanned`'ı artırmaz ve gerekçesiyle sayılır.

`domain_state=FANOUT_APPROVED` bir kapı açmaz; kapıyı açan
`gate.verdict=GREEN`'dir ve o da coverage aritmetiğine tabidir.

---

## 2. Kâr kapısı — dört test, hepsi geçmeli

Sıra bağlayıcıdır: 1 ve 2 güvenlik, 3 ekonomi, 4 mekanik. Biri düşerse
kalanlar koşmaz ve karar yazılır.

### (a) Test 1 — Bağımsızlık

Dal M, dal N'in **o koşuda üreteceği** bir artifact'i okuyorsa bağımsız
değildir. `reads[N] ∩ writes[M] == ∅` her (M, N) çifti için ölçülür.

Düşerse: `FANOUT_UNSAFE`, iş seri zincire döner. Sıralı ve bağımlı işi paralel
dallara bölmek, durumu dosyalar üzerinden elden ele taşımaya dönüşür; tek
oturum bunu daha az hatayla yapar.

### (b) Test 2 — Yazar-ayrıklığı

Tüm dalların `writes[]` kümelerinin **ikili kesişimi boş** olmalı. Kesişim
"muhtemelen çakışmaz" diye geçilemez; §3'teki kanıt üretilir.

Ek olarak `writes[]` içinde `.kicad_pcb` / `.kicad_sch` **varsa** karar
otomatik `FANOUT_UNSAFE`'tir — o dosyalara tek ajan yazar ve kararın sahibi bu
skill değil, board-write-lock'tur.

### (c) Test 3 — Kâr

Bu testte eşik **yok**, çünkü ölçülmüş bir eşik yok
([[references/execution-envelope.md]] §3: aranan ve bulunamayan sayılar).
Yerine dört soru var ve ikisi **sebep**, ikisi **uygulanabilirlik**:

*Sebep — en az biri "evet" olmalı:*

1. Dal, ana bağlama **geri dönmeyecek hacimli** çıktı mı üretiyor (test log'u,
   DRC JSON'u, netlist dökümü)? Hacimli çıktının izole bağlamda kalması
   fan-out'un ana kazancıdır.
2. Dalın işi, ana ajanda **yüklü olmayan** bir bağlam mı gerektiriyor?
   Zaten yüklü bağlam için fan-out onu yeniden toplatır.

*Uygulanabilirlik — ikisi de "evet" olmalı:*

3. Sonuç, ana bağlama **kısa bir özet** olarak dönebilir mi? Ham çıktı geri
   istenecekse izolasyonun faydası yoktur.
4. Birim **kendi başına bir teslim** üretiyor mu (bir rapor, bir receipt, bir
   dosya)? Üretmiyorsa koordinasyon maliyeti kazancı yer.

Kural: `(1 veya 2) ve (3 ve 4)`. Sebep yoksa ya da uygulanabilirlik düşerse
karar `INLINE_CHEAPER`'dır. Hacimli çıktı **tek başına** yetmez: özet olarak
dönemeyen bir dal, ana ajanı ham çıktıyı geri istemeye zorlar ve izolasyon
kazancı orada kaybolur.

Genişlik: **3–5 dal** ile başla. Üç odaklı dal, beş dağınık daldan iyidir; bu
aralığın üstü bu repoda ölçülmedi. Gecikme kritikse inline kal — alt ajan taze
bağlamla başlar ve bağlam toplaması zaman alır.

Token tarafında yayımlanmış tek oran şudur: ajan koşusu sohbete göre ~4×,
çok-ajanlı sistem sohbete göre ~15× token. **Bu oran çok-ajanlı ile tek-ajanlı
arasındaki oran değildir**; ikisini birbirine bölerek yeni bir sayı türetme
([[references/execution-envelope.md]] §2, `CONFIRM` seviyesinde: snapshot
alınmadı).

### (d) Test 4 — Yürütme zarfı

- Beklenen süre foreground zarfını aşıyorsa koşu arka plana alınır **ve** kendi
  rapor dosyasını yazar. Verdict stdout'tan değil, `--json` ile yazılan
  dosyadan okunur — repo sözleşmesi zaten `--json`'un daima yazmasını ister.
- Çökebilen/asılabilen adım (`pcbnew` SWIG, MCP board backend) izole
  alt-süreçte koşar: `recovery.py --timeout-s`. Bir çökme **bir** alt-süreci
  kaybeder, oturumu değil.
- Her dalın çıktısı ayrı yola gider (§3). Ortak rapor dosyası yoktur.

---

## 3. Yazar-ayrıklığı kanıtı — yol düzeni ve uzlaştırma

Kanıt üç parçadır ve üçü de `fanout_plan.json`'a yazılır: taranan yol sayısı,
bulunan kesişimler, kullanılan yöntem.

Yol düzeni — **bir yola tam olarak bir dal yazar**:

```
runs/<run_id>/
  fanout_plan.json          # kapı kararı (bu skill)
  <branch_id>/
    report.json             # dalın kendi raporu — tek yazar
    stdout.log
  reconcile/
    merged.json             # yalnız seri uzlaştırma adımı yazar
```

`runs/<run_id>` yayımı [[../gptpro/SKILL|gptpro]] §6'nın atomik run-id
kalıbını izler; bu skill yeni bir yayım kalıbı **tanımlamaz**.

Uzlaştırma (reconcile) **seridir** ve tek yazarı vardır. Oraya kadar hiçbir dal
başka dalın dosyasını okumaz, yoksa Test 1 kâğıt üstünde kalır.

`git` bu düzenin dışında değildir: index tek dosyadır, iki dal aynı anda commit
edemez. Commit/tag uzlaştırma adımında, seri olarak yapılır — üstelik bunlar
geri alınamayan yan etkidir (board-write-lock §4).

---

## 4. Nüanslar — bilinen tuzaklar

### (a) Atomik yazım *torn* dosyayı önler, *lost update*'i önlemez

`common/report_io.py::atomic_write_text` geçici dosya + `fsync` + `os.replace`
yapar (`tools/common/report_io.py:46-68`), yani okuyucu asla yarım dosya
görmez. Bu **eşzamanlı oku-değiştir-yaz'ı korumaz**: iki dal aynı JSON'u okuyup
kendi sürümünü yazarsa biri sessizce kaybolur ve iki çıktı da tek başına doğru
görünür. Tek yazar-tek dosya + uzlaştırma bunun çaresidir; kilit değil, düzen.

### (b) KiCad IPC/MCP çağrıları paralelleştirilmez

`docs/tool-inventory.md:342` bunu araç ön koşulu olarak zaten yazıyor: *"Yalnız
açık GUI oturumu; IPC çağrılarını paralelleştirme"*. Eşzamanlı çağrı güvenliği
bu repoda **ölçülmedi**; varsayılan seridir ve bu kapı onu değiştirmez.

### (c) Alt-ajan self-report'u kanıt değildir

Kritik metriği ana ajan aynı araç ve input hash'iyle **yeniden ölçer**; bu
kuralın sahibi [[../pcb-orchestrator/SKILL|pcb-orchestrator]] ve
[[../decompose-pcb-decisions/SKILL\|decompose-pcb-decisions]]'dır. Buradaki tek
ek: fan-out yeniden ölçme maliyetini **dal sayısıyla çarpar**. Test 3'ün 4.
sorusuna "hayır" diyen bir birim, yeniden ölçüm maliyetini de beraberinde
getirir.

### (d) Mesajsız kapanan dal "başarısız" değildir

Ajan rapor vermeden kapanırsa transkript kurtarılır; bulunursa provenance ile
kaydedilir, bulunmazsa görev yeniden açılır. Sessizliği FAIL'e, self-report'u
kanıta çevirme (pcb-orchestrator §Alt-ajan kapanış).

### (e) Salt-okunur dal için izolasyon bedava değil ve izole edecek şeyi yok

İzole çalışma kopyası dosya **yazımını** izole eder. Yazmayan bir dal için
kazanç sıfırdır, maliyet gerçektir: taze checkout gitignore'lanmış girdileri
taşımaz — bu repoda `.venv/`, `fp-info-cache`, `*.lck`, `*-backups/` ve
`gptpro-bundles/` (`.gitignore`) taze kopyada **yoktur**. KiCad önbelleği
olmadan koşan bir dal "eksik kütüphane" diye rapor eder ve bu sahte bir
bulgudur.

### (f) İki dal aynı hesabı yapıyorsa raporlar byte-özdeş olmalı

Değilse cevaba bir şey sızıyor (dict sırası, temp dosya adı, duvar saati,
tohumsuz RNG) ve dalların çıktıları karşılaştırılamaz. `benchmark.py`
determinizmi ayrı bir eksen olarak ölçer; `--allow-nondeterminism` bir çare
değil, bir itiraftır.

### (g) Dosya ayrıklığı sözdizimseldir, karar ayrıklığı semantiktir

İki dal ayrı dosyaya yazıp **çelişen** kararlar verebilir (aynı rail'e iki
farklı limit, aynı nete iki farklı sınıf). Bu kapı onu yakalamaz; kalite
kapısı ve gate receipt indirgemesi yakalar. Ayrıklık kanıtı bir "kararlar
tutarlı" iddiası değildir.

---

## 5. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek hızlanma (wall-clock kazancı) | `NO_COVERAGE` | Bu repoda **hiç ölçülmedi**; süre iddiası ancak ölçülürse kanıttır |
| Minimum iş boyutu eşiği (saniye/token/dosya) | — | Yayımlanmış **ölçülmüş** eşik yok; kapı nitel dört soruyla kurulur (§2c) |
| Token maliyeti | B | Ölçülebilir ama bu repoda ölçülmüyor; §2c'deki oran dış kaynak ve `CONFIRM` |
| KiCad MCP/IPC'nin eşzamanlı çağrı güvenliği | `NO_COVERAGE` | Hiçbir belge söylemiyor, bu repoda denenmedi → varsayılan seri (§4b) |
| Semantik çakışma (ayrı dosya, çelişen karar) | C | Yol kesişimi boş olsa da kararlar çelişebilir (§4g) |
| Board/şematik kilidi kararı | — | Sahibi [[../pcb-orchestrator/references/board-write-lock.md\|board-write-lock]]; bu skill yalnız kanıt üretir |
| Hangi DAG düğümünün paralel olduğu | — | Sahibi [[../pcb-orchestrator/references/phase-dag.md\|phase-dag]] |
| Harness zarfı sayılarının kalıcılığı | `CONFIRM` | Sürüme bağlı ve snapshot'sız; [[references/execution-envelope.md]] §4 bunu neden `AUTHORITATIVE_SOURCE` saymadığını yazar |

> **AÇIK MADDE (2026-08-11, ölçüm).** board-write-lock §5'in "paralel koşabilir"
> listesi eksik: 23 skill'in 5'i `writes_board: true`, yani **18** salt-okunur
> skill var; liste **12** tanesini sayıyor. Sayılmayan 6: `design-checker`,
> `ecad-varlik-kalifikasyonu`, `gptpro`, `ilk-urun-montaj-kabulu`,
> `l3-bringup-karakterizasyon` ve `paralel-yurutme` (bu skill). Liste o dosyanın
> **sahipliğindedir**; burada düzeltilmedi, çünkü ikinci bir sahip bu deponun
> tekrar tekrar bulduğu drift sınıfıdır. §6'daki komut listeyi frontmatter'dan
> yeniden üretir ve regresyon **tehlikeli yönü** kapatır: board yazan bir
> skill'in seri listeden düşmesi testi kırar
> (`test_paralel_yurutme.py::ReadOnlySkillCountIsMeasured`). Eksik salt-okunur
> adların eklenmesi board-write-lock sahibine aittir.

---

## 6. Doğrulama

```bash
cd pcb-agent

# kapının kendi regresyonu: ayrıklık, aritmetik, fault-injection
python3 -m unittest tools.tests.test_paralel_yurutme

# salt-okunur / board-yazan ayrımı — YALNIZ frontmatter'dan (elle kopyalanmaz)
for f in skills/*/SKILL.md; do
  awk '/^---$/{n++; next} n==1 && /^writes_board:/{print $2}' "$f"
done | sort | uniq -c

# izole koşu + zaman aşımı zarfı çalışıyor mu
python3 tools/scripts/memory/recovery.py --self-test

# dal raporu bağımsız yeniden ölçülebiliyor mu
python3 tools/scripts/independent_verify.py --self-test

# determinizm ekseni (§4f) canlı mı
python3 tools/scripts/benchmark.py --self-test
```

Ölçüm (2026-08-11): beş komut da yeşil; `writes_board` sayımı **18 salt-okunur
/ 5 board-yazan** (toplam 23 skill).

> **Gövdeye bakan sayaç neden kullanılmıyor — ölçülmüş sahte sayı
> (2026-08-11).**
> `grep -l 'writes_board: true' skills/*/SKILL.md | wc -l` **6** verir; doğru
> sayı **5**'tir. Fazlalık bu skill'in kendisidir: gövdesinde board-write-lock'a
> atıf yaparken o dizeyi anıyor. Yani *devreden* bir skill, *sahiplenen* bir
> skill gibi sayılıyor. Kapsam yalnız frontmatter'dan okunur; aynı tuzağı
> regresyon da kapatır (`declared_writes_board`).

**Fault injection** — kapı yalnız bunları *yakalayabildiği* için kapıdır:

| Enjeksiyon | Beklenen |
|---|---|
| İki dalın `writes[]` listesine aynı yolu koy | `FANOUT_UNSAFE`, `violations = 1` |
| Bir dala `.kicad_pcb` yazdır | `FANOUT_UNSAFE` (board-write-lock'a devir) |
| Dal M'nin `reads[]`'ine dal N'in çıktısını koy | `FANOUT_UNSAFE` (bağımsızlık) |
| `writes[]` alanını hiç yazma | O birim `excluded` + gerekçe; `scanned` artmaz |
| `eligible` sayısını elle şişir | `INVALID_COVERAGE/RED` |
| Aday listesini boşalt | `NO_COVERAGE` (asla `GREEN`) |
| Dört sorunun hepsine "hayır" | `INLINE_CHEAPER` — bu bir hata değil, kapının çalıştığının kanıtı |

---

## 7. Kabul kriterleri

- [ ] Fan-out kararı **dal başlatılmadan önce** verildi ve `fanout_plan.json`
      olarak yazıldı.
- [ ] Dört test **sırayla** koşuldu; düşen testte kalanlar koşulmadı ve
      gerekçe yazıldı.
- [ ] Yazar-ayrıklığı **ölçüldü**: taranan yol sayısı, bulunan kesişimler ve
      yöntem receipt'te duruyor. "Muhtemelen çakışmaz" yazılmadı.
- [ ] `writes[]` içinde `.kicad_pcb`/`.kicad_sch` bulunan hiçbir birim dala
      verilmedi; karar
      [[../pcb-orchestrator/references/board-write-lock.md\|board-write-lock]]'a
      devredildi.
- [ ] Her dal **kendi** yoluna yazdı; `runs/<run_id>/<branch_id>/` dışında
      ortak rapor dosyası kullanılmadı.
- [ ] Uzlaştırma seri koştu ve `merged.json`'un tek yazarı oldu.
- [ ] `git` commit/tag paralel dalda **yapılmadı**; uzlaştırma adımına
      bırakıldı.
- [ ] Foreground zarfını aşan koşu arka plana alındı **ve** kendi rapor
      dosyasını yazdı; verdict stdout'tan okunmadı.
- [ ] Çökebilen adım izole alt-süreçte, `--timeout-s` ile koştu.
- [ ] Alt-ajan self-report'u kanıt sayılmadı; kritik metrik ana ajanda
      yeniden ölçüldü.
- [ ] `INLINE_CHEAPER` kararı bir başarısızlık gibi raporlanmadı ve gerekçesi
      yazıldı.
- [ ] Hızlanma iddiası **yapılmadı** ya da ölçüldü. Ölçülmemiş süre kazancı
      receipt'e yazılmadı.
- [ ] `parallelism_coverage` adı bu skill'in metriği için kullanılmadı
      ([[../_shared/glossary.md]] §8).
- [ ] Bu skill hiçbir board/şematik dosyasına yazmadı.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

---

## İlgili

[[../pcb-orchestrator/SKILL|pcb-orchestrator]] ·
[[../pcb-orchestrator/references/board-write-lock.md]] ·
[[../pcb-orchestrator/references/phase-dag.md]] ·
[[../decompose-pcb-decisions/SKILL|decompose-pcb-decisions]] ·
[[../otonom-routing/SKILL|otonom-routing]] ·
[[../claim-audit/SKILL|claim-audit]] ·
[[../design-checker/SKILL|design-checker]] ·
[[../gptpro/SKILL|gptpro]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

---

## Araç kancaları (2026-08-11)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`context-pack`** — Her dal için ayrı manifest; dal kapandığında
  `--postflight` ile o dalın çıktısı doğrulanır. Manifest sözleşmesinin sahibi
  decompose §2.3'tür, bu skill onu dal başına **çağırır**.
- **`gate-receipt`** — Dalların receipt'lerini birleşik kapıya indirgerken;
  hash uyuşmazlığı `STALE` verir, yani bir dal eski girdiyle koşmuştur.
- **`recovery`** — Çökebilen/asılabilen adımı izole alt-süreçte koşturmak için
  (`--timeout-s`). Rapor, "merdiven tükendi" ile "merdiven hiç kurulmadı"yı
  ayırır; ikincisi bir fan-out başarısızlığı değildir.
- **`independent-verify`** — Bir dalın board iddiasını ana ajan farklı bir
  okuyucuyla yeniden ölçerken (KiCad DRC/ERC'den bağımsız).
- **`benchmark`** — Bir adımı paralel dala vermeden önce `--repetitions` ile
  determinizmini ölçmek için; byte-özdeş olmayan rapor §4f'nin konusudur.
