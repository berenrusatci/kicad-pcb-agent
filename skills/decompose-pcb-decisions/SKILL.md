---
name: decompose-pcb-decisions
description: Uzun bağlamlı veya çok disiplinli PCB tasarımlarını küçük, bağımlılıkları açık, kanıta bağlı karar birimlerine ayırır ve her ajan için hash-doğrulamalı minimum context pack üretir. Halüsinasyonu, bayat bağlamı ve gerekçesiz varsayımları azaltmak; bir tasarım görevini alt ajanlara güvenle devretmek için kullan.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 0 — karar ve context kapısı (DAG düğümü açılmadan önce)"
assurance_loop: [L0, L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: kullanıcı hedefi, schema: null, required: true}
  - {artifact: config/tool_registry.json, schema: null, required: true}
outputs:
  - {artifact: design_intent.json, schema: references/decision_units.schema.json}
  - {artifact: decision_units.json, schema: references/decision_units.schema.json}
  - {artifact: context_manifest.json, schema: references/decision_units.schema.json}
  - {artifact: context_check.json, schema: null}
  - {artifact: postflight_check.json, schema: null}
writes_board: false
tools: [context-pack, block-catalog]
---

# PCB kararlarını ayrıştır

Ortak kapı kuralları: [[../_shared/coverage-gate.md]]

## 0. Ne zaman çağrılır

- **Her DAG düğümünden önce.** [[../pcb-orchestrator/SKILL|orchestrator]] Faz 0'da
  bu skill'i çağırmadan hiçbir worker açmaz.
- Bir tasarım görevi alt ajana devredilecekse (bağlam bütçesi, disiplin ayrımı).
- Bir girdi değiştiğinde: paket **büyütülmez**, eskisi geçersiz kılınıp yeniden
  üretilir (§5).
- Mimari birleşim gerektiğinde (§6).

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| kesinleşmiş hedef / kısıt / kapsam dışı | kullanıcı | çözülmemiş seçim `open_questions`'a; karar gibi yazma |
| `owner_skill` | skill listesi | birim açılamaz |
| `depends_on` kimlikleri | karar grafiği | çevrim varsa `context_pack.py` bloklar |
| tool adı + hash | `config/tool_registry.json` | kayıt dışı tool → worker açılmaz |
| kanıt kimliği | canonical evidence kataloğu (typed, hash/locator bağlı) | serbest metin "datasheet" **kanıt sayılmaz** |

Karar birimi alanları ve örnekleri:
[`references/decompose-pcb-decisions-contract.md`](references/decompose-pcb-decisions-contract.md) ·
şema: [`references/decision_units.schema.json`](references/decision_units.schema.json)

## 2. Adımlar

### 2.1 Niyeti dondur
Önce `design_intent.json` üret. Yalnız kullanıcıca kesinleşmiş hedefleri,
kısıtları ve kapsam dışı maddeleri yaz. Çözülmemiş seçimi karar gibi yazma;
`open_questions` altında tut. Stereo/yapılandırılmış ışık gibi sahne koşuluna
bağlı bir seçimi sessizce kapatma.

### 2.2 Karar grafiğini çıkar
Her kararı tek test edilebilir soruya indir ve `decision_units.json` içinde tut:

- Bir birim tek `owner_skill` ve tek çıktı sözleşmesi taşısın.
- Birim, başka birimin gerekçesini kopyalamasın; yalnız `depends_on`
  kimliklerini taşısın.
- `status` yalnız `blocked`, `ready`, `proposed`, `accepted`, `rejected` olsun.
- `accepted` karar için kaynak/ölçüm kimliği ve invalidation trigger zorunlu olsun.
- Kanıt kimliği yalnız typed, hash/locator bağlı canonical evidence kataloğuna
  işaret etsin; serbest metin "datasheet" kanıt sayılmasın.
- Aynı birim hem güç bütçesi hem stackup hem routing kararı vermesin; sınırda
  yeni bir birim aç.

### 2.3 Minimum bağlam paketini kur
Her aktif karar için ayrı `context_manifest.json` üret. Yalnız şunları ekle:

1. görev hedefi, kapsamı ve non-goal'ları;
2. ilgili karar kimliği ve doğrudan bağımlılıkları;
3. hash'li `design_intent`, karar birimi ve `tool_registry` girdileri;
4. izinli araçlar, eylemler ve yazım yasakları;
5. beklenen çıktı şeması ve deterministik kapılar;
6. bloklayan açık sorular.

Ham sohbet geçmişini, tüm vault'u, başka disiplinlerin skill'lerini ve eski
ajan raporlarını pakete **koyma**. Bir dosya yalnız "yararlı olabilir" diye
değil, çıktıdaki belirli bir alanı etkilediği için eklenir.

### 2.4 İddiayı model cevabından ayır
Worker çıktısındaki her teknik iddiada şunları zorunlu kıl:
`claim_id`, `statement`, `status`, `evidence_ids`, `confidence`, `scope`.

- `evidence_ids=[]` ise teknik iddia `accepted` olamaz; `unverified` bırak.
- Araç self-report'unu kanıt sayma. Kaynak dosyayı ana ajan aynı araç ve input
  hash'iyle yeniden ölçsün.
- "Doğrulanamadı"yı "yanlış"a çevirmesin.
- Datasheet/veri yoksa parametre veya API adı uydurmasın; `TBD`/`CONFIRM` üretsin.
- A/B/C/D kanıt seviyesini [[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ile
  belirlesin. C veya D kararı yazılım PASS'i gibi sunmasın.

`official_source` evidence'inda `document_id`, `revision`, `publication_date`,
`locator`, `accessed`, `snapshot_path`, `snapshot_sha256` ve exact claim scope
zorunludur; snapshot hash'ini dosyaya karşı yeniden hesapla. Mandatory
postflight'ta exit code'a ek olarak hash-bağlı raporun
`effective_status == GREEN` olduğunu doğrula. Nonzero `expected_exit` yalnız
`negative_fixture` rolünde kabul edilir.
`evidence_origin=AUTHORITATIVE_SOURCE+TOOL_REPORT`, `assurance_loop=L0+L1`,
`status=CONFIRM` (snapshot/verdict bağı eksikse).

### 2.5 Bağlamı yenile, büyütme
Bir girdi değişince paketi ekleme yaparak şişirme. Eski paketi geçersiz kıl,
hash'leri güncelle ve yeniden üret. Downstream yalnız güncel paketi tüketsin.
Önceki rationale gerekiyorsa karar kaydına tek cümlelik `supersedes` bağı koy.

### 2.6 Mimari birleşimi yönet
Alt kararları ancak her child gate güncelse birleştir. Birleşim ajanı yeni
elektriksel gerçek icat etmesin; seçenekler arasındaki trade-off'u ve açık
blocker'ları sunsun. Mimari kararın sonucu en az şunları taşısın:

- kabul edilen seçenek ve reddedilen seçeneklerin gerekçeleri;
- hangi kanıtla kapandığı;
- hangi olayda yeniden açılacağı;
- downstream artifact ve skill sahipleri;
- insan/field-solver/lab sign-off gerektiren sınırlar.

### 2.7 Board yazımını ayır
Araştırma ve karar ayrıştırma **salt-okunur** kalsın. `.kicad_sch`/`.kicad_pcb`
yazımı ayrı transaction ve tek sahipli olsun. Context pack yazma yetkisi
vermedikçe worker board'u değiştirmesin
([[../pcb-orchestrator/SKILL|orchestrator]] §Board yazımı kilidi).

## 3. Nüanslar — bilinen tuzaklar

### (a) `READY` ≠ "worker çıktısı doğru"
`READY`, yalnız **minimum bağlamın göreve hazır olduğunu** söyler. Worker
çıktısının kabul edilmesi `--postflight` + `ACCEPTED` ister.

### (b) Gate validator atıl bir argüman olmamalı
`context_pack.py` postflight, bildirilen argv'yi **gerçekten çalıştırır**.
Manifest'te yazılı gate komutu gerçek entrypoint değilse araç bloklar — yani
"gate tanımladım" demek yetmez, gate'in koştuğu kanıtlanır.

### (c) Paket bütçesi bir kalite kapısıdır
Dosya sayısı / byte bütçesi aşılıyorsa sorun bütçede değil **ayrıştırmadadır**:
birim çok geniştir, ikiye böl.

### (d) Kayıt dışı tool varsayma
İzinli tool listesi dışında araç varsayılamaz. Tool adı hash'li registry'de
kayıtlı ve `available` olmalıdır
(`config/tool_registry.json`, `tools/scripts/sync_tool_registry.py` üretir).

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Teknik iddianın doğruluğu | — | Bu skill **süreç** kapısıdır; elektriksel gerçeği doğrulamaz |
| Kullanıcı niyetinin eksiksizliği | C | Blocking soru bırakılır, tahmin edilmez |
| Alt ajanın gerçekten ölçtüğü | — | Ana ajan aynı tool + input hash'iyle **yeniden ölçer** |
| C/D seviyesi bilinmeyenler | C/D | Açık kalır; LLM tarafından kapatılamaz |

## 5. Doğrulama

```bash
# preflight — yalnız exit 0 + READY görev açar
python3 tools/scripts/context_pack.py context_manifest.json \
  --root . --json context_check.json

# postflight — yalnız exit 0 + ACCEPTED downstream kararı açar
python3 tools/scripts/context_pack.py context_manifest.json \
  --root . --postflight --json postflight_check.json

python3 -m unittest -v tools.tests.test_context_pack
```

Exit kodları: `0` = READY/ACCEPTED · `1` = eksik/çelişkili bağlam veya blocking
soru · `2` = bozuk sözleşme / çalıştırma hatası.

> `--json` **yazar** (rapor çıktısı). `--postflight` bildirilen argv'yi
> çalıştırdığı için **MCP üzerinden asla yayımlanmaz**.

Fault-injection: manifest'e kayıt dışı bir tool ekle → preflight **FAIL vermeli**.
Gate argv'sini gerçek entrypoint olmayan bir komutla değiştir → postflight
**bloklamalı**.

## 6. Kabul kriterleri

- [ ] Her karar tek soruya ve tek sahibine (`owner_skill`) indirgenmiş.
- [ ] Hiçbir birim aynı anda güç + stackup + routing kararı vermiyor.
- [ ] Birimler başka birimin gerekçesini kopyalamıyor; yalnız `depends_on` taşıyor.
- [ ] `status` yalnız izinli beş değerden biri; `accepted` kararlar kaynak/ölçüm
      kimliği **ve** invalidation trigger taşıyor.
- [ ] Blocking soru varken task `READY` değil.
- [ ] Tüm zorunlu girdilerin ve yerel araçların SHA-256'sı güncel; tool adı
      hash'li registry'de kayıtlı ve `available`.
- [ ] Paket dosya/sayı/byte bütçesini aşmıyor.
- [ ] İzinli tool listesi dışında araç varsayılmıyor.
- [ ] Ham sohbet geçmişi / tüm vault / başka disiplin skill'leri pakete girmemiş.
- [ ] Çıktı `claim_id → evidence_ids` bağı ve deterministik gate taşıyor;
      `evidence_ids=[]` olan teknik iddia `accepted` değil.
- [ ] `official_source` evidence'ları tam provenance taşıyor ve snapshot hash'i
      dosyaya karşı yeniden hesaplanmış.
- [ ] Postflight **gerçek worker çıktısını** parse etmiş ve exact argv gate'lerini
      koşmuş; `effective_status == GREEN` doğrulanmış.
- [ ] Gate validator exact argv'nin **gerçek entrypoint'i**; atıl bir argüman değil.
- [ ] Girdi değişiminde paket büyütülmemiş, yeniden üretilmiş; `supersedes` bağı var.
- [ ] Board yazımı ayrı transaction; context pack yetki vermedikçe worker board'a
      dokunmamış.
- [ ] C/D seviyesi bilinmeyenler açık; LLM tarafından kapatılmamış.

## İlgili

[[../pcb-orchestrator/SKILL|orchestrator]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../claim-audit/SKILL|claim-audit]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`block-catalog`** — Mimari kararından **parametrik devre bloğu** örnekler ve tasarımı ona karşı denetler.
