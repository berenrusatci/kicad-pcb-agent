---
name: gptpro
description: Export a repo into review-ready zip bundles for a non-agentic frontier model (GPT Pro on the ChatGPT web UI, or any chat model with file upload), with a coverage-gated snapshot contract — per-bundle file/byte accounting, exclusion inventory, per-file SHA-256, in-zip MANIFEST.json, out-of-set handoff_receipt.json, atomic run-id publish, and a disclosure gate kept separate from the credential scan. Use for packaging a repo for external LLM review or audit. Pairs with the `gptpro-handoff` workflow skill.
version: 2.0.0
last-reviewed: 2026-08-10
phase: "Faz-dışı — handoff snapshot / dağıtım kapısı"
assurance_loop: [L0, L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: bundles.conf, schema: bundles.example.conf, required: true}
  - {artifact: "disclosure-policy.json", schema: references/disclosure-policy.example.json, required: false}
outputs:
  - {artifact: "<prefix>-<bundle>.zip", schema: null}
  - {artifact: "MANIFEST.json", schema: references/manifest.schema.json}
  - {artifact: handoff_receipt.json, schema: references/handoff_receipt.schema.json}
  - {artifact: "00-OKUNACAK.md", schema: null}
writes_board: false
tools: [gptpro-export, gptpro-export-engine]
---

# SKILL: GPTPro export (handoff snapshot kapısı)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Workflow eşi: [[../../researchGpt/gptpro-handoff/SKILL|gptpro-handoff]]

> **Neden bu bir depo skill'i.** Bu araç artık yardımcı bir zip script'i değil;
> dış denetçinin hangi kanıta erişeceğini belirleyen bir **assurance
> boundary**. Bir dosyayı paketten düşürmek, o dosya hakkındaki her dış bulguyu
> önceden imkânsız kılar. Deponun geri kalanı "ölçüm → kapsam → köken →
> makbuz" zincirini zorunlu tutarken paketleme katmanının bu zincirin dışında
> kalması, tam olarak ölçülmeyen yerdi.
>
> **Araçlar nerede duruyor.** `scripts/` bu skill'in kendi dizininde;
> `config/tool_registry.json` hem `export.sh`'i (`gptpro-export`) hem motoru
> (`gptpro-export-engine`) locator + SHA-256 ile kayda alıyor. İkisi de kayıtlı,
> çünkü yalnız kabuğu bağlamak motoru serbest bırakırdı: motor değişirken
> registry "senkron" demeye devam ederdi.
>
> Registry üreteci (`tools/scripts/sync_tool_registry.py`) bir dizin tarayıcısı
> değil, açık bir liste; locator'lar `pcb-agent/` köküne göreli. Bu yüzden
> script'leri `tools/scripts/` altına taşımak gerekmedi — skill kendi ağacında
> bütün kalıyor ve hash bağı yine de mekanik.

## 0. Ne zaman çağrılır

- Dış modele (GPT Pro vb.) gönderilecek korpus ilk kez üretilirken.
- `gptpro-handoff` adım 3'te snapshot tazeliği bozulduğunda — `--recheck`
  `STALE` derse.
- `bundles.conf` değiştiğinde: kapsam sözleşmesi değişmiştir, eski makbuz o
  sözleşmeyi tarif etmez.

## 1. Otorite sınırı — bu skill neye yetkilidir

**Yetkili olduğu tek hüküm:** *bu snapshot şu declared scope'u şu hash'lerle
taşıyor.*

| Soru | Sahibi |
|---|---|
| Bir ERC/DRC/board kuralı doğru mu | ilgili adım skill'i + [[../dogrulama-matrisi/SKILL\|dogrulama-matrisi]] |
| Bir kuralın seviye/coverage/authority sınıfı | [[../dogrulama-matrisi/SKILL\|dogrulama-matrisi]] |
| Dış modelin bulgusu doğru mu | [[../claim-audit/SKILL\|claim-audit]] |
| Üretilen tasarım fab'a gidebilir mi | [[../design-checker/SKILL\|design-checker]] |
| Bu korpus beyan edilen kapsamı taşıyor mu | **bu skill** |

`GREEN` üretmesi kartın doğru olduğu anlamına **gelmez**. `handoff_receipt.json`
bunu `verdict_scope` alanında kendi metniyle söyler.

## 2. Girdi/çıktı sözleşmesi

```bash
BUNDLES_CONF=<repo>/.gptpro/bundles.conf \
PROJECT_ROOT=<repo> \
OUTPUT_DIR=<repo>/gptpro-bundles \
BUNDLE_PREFIX=camera-pcb \
bash pcb-agent/skills/gptpro/scripts/export.sh
```

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `PROJECT_ROOT` | `$PWD` | export edilecek depo |
| `OUTPUT_DIR` | `./gptpro-bundles` | `runs/<snapshot_id>/` ve `current` sembolik bağı buraya yazılır |
| `BUNDLE_PREFIX` | depo dizin adı | zip ön eki |
| `BUNDLES_CONF` | script yanındaki `bundles.conf` | paket tanımları; yoksa autodetect |
| `DISCLOSURE_POLICY` | `<repo>/.gptpro/disclosure-policy.json` | dağıtım yetkisi kaydı (§5) |
| `ALLOW_SECRETS` | `0` | `1` HARD credential eşleşmesini geçer; koşu artık GREEN olamaz |
| `KEEP_RUNS` | `5` | `runs/` altında tutulacak koşu sayısı |
| `GPTPRO_MAKER_ID` / `GPTPRO_CHECKER_ID` | boş | makbuza yazılan maker/checker kimlikleri (§6) |

Çıktılar `OUTPUT_DIR/runs/<snapshot_id>/` altında, `current` oraya bağlanır:
sekiz zip, `handoff_receipt.json`, üretilmiş `00-OKUNACAK.md`. Her zip'in kökünde
`MANIFEST.json` vardır.

## 3. Kapsam aritmetiği — paket başına, dosya **ve** bayt

Her paket için `eligible / scanned / excluded / violations` yayımlanır ve
`scanned + excluded == eligible` **iddia değil, kontrol edilir**: tutmuyorsa
koşu `INVALID_COVERAGE` ile durur ve hiçbir şey yayımlanmaz.

Bunu mümkün kılan yapısal karar: aynı geçiş hem dosyayı stage eder hem sayar
(`scripts/gptpro_export.py`). rsync kopyalayıp başka bir bileşen sayarken
aritmetik, başka bir programın davranışı hakkında bir *iddiadır*; burada olan
şeyin *ölçümüdür*.

`scanned = 0` → `NO_COVERAGE`. Asla `GREEN` değil.

### Dışlama envanteri

Her dışlanan dosya için `pattern`, `policy_source`, `reason`, `count`, `bytes`
kaydedilir; manifestte `(pattern, policy_source)` çiftinde toplanır.
`policy_source` üç değerden biridir — `tool_global` (aracın kendi varsayılanı),
`project_config` (bu deponun kararı), `source_decl` (paketin kendi bildirimi,
genelde kapsamı kardeş bir paket taşıdığı için).

Model-yüzü özette dışlanan dosyaların **adları yoktur**, sınıf/örüntü ve sayı
vardır: saklanan bir dosya hakkında okuyucuya borçlu olunan şey adı değil, var
olduğudur. Tam liste operatörün makbuzunda kalır.

## 4. Eksik kaynak fatal'dır

Elle yazılmış config'te eksik bir `dir:`/`file:` kapsam kaybıdır. Eski davranış
`WARN` + `continue` idi: paket eksik girdilerle başarıyla üretiliyordu ve dış
model "yok" ile "ölçülemedi" arasındaki farkı göremiyordu.

Artık: `NO_COVERAGE`, koşu durur, önceki set olduğu gibi kalır. Varyanta bağlı
bir kaynak varsa `optional:` önekiyle bildirilir — o zaman "yok" kararı
manifestte `disposition: optional, present: false` olarak **görünür**.

**`ALLOW_MISSING` yoktur ve eklenmemelidir.** Bir kez kullanılan bypass her
koşu kullanılan bypass olur; sert kuralı gizli bir çevre değişkeniyle delmek,
onu hiç koymamaktan daha kötüdür çünkü kapı hâlâ varmış gibi görünür.

## 5. İki ayrı tarama — ve neden ayrı

| Kapsam | Ölçtüğü | Ölçmediği |
|---|---|---|
| `credential_scan` | staged baytlarda canlı-görünümlü sağlayıcı kimlik biçimi var mı | korpusun üçüncü tarafa çıkmaya yetkili olup olmadığı |
| `content_policy` | hangi kaynak türüyle geldiğinden bağımsız olarak yasaklı dosya sınıfı stage'e ulaştı mı | aynı |
| `disclosure_gate` | dağıtım yetkisi kaydı staged dosyaların tamamını kapsıyor mu | dosyanın hukuken paylaşılabilir olduğu |

Eski araç `secret scan clean` yazıyordu ve okuyucu bunu "yüklemek güvenli" diye
duyuyordu. İkisi farklı iddiadır ve yalnız biri ölçülmüştü.

**`content_policy` staging'den SONRA, tüm dosyalara uygulanır.** Eskiden
dışlamalar bir rsync bayrağıydı ve `file:` kaynaklar düz `cp` ile geliyordu:
aynı `.pdf` `dir:` üzerinden yasak, `file:` üzerinden serbestti. Bir dosyanın
işlemi nasıl bildirildiğine değil ne içerdiğine bağlı olmalı — **inclusion
mekanizması bir politika değildir.**

**`disclosure_gate` ve K-1.** Bir regex "bu dosya hukuken paylaşılabilir"
diyemez. Yapabileceği tek şey, birinin yazdığı bir kararın staged korpusu ne
kadar kapsadığını ölçmektir. O karar `.gptpro/disclosure-policy.json`'dadır;
format [`references/disclosure-policy.example.json`](references/disclosure-policy.example.json).

`share` **üç** değerlidir, iki değil:

| değer | anlamı | kapı |
|---|---|---|
| `allow` | çıkabilir | GREEN'e engel değil |
| `deny` | çıkamaz | `RED` — export durur, hiçbir şey yayımlanmaz |
| `confirm` | sınıflandırıldı, cevap "otorite tanımsız" | `CONFIRM`, upload kapalı |

`confirm` bedavaya gelmiyor, işini yapıyor. O olmasaydı, kimsenin temize
çıkarma yetkisi olmayan bir sınıfın iki evi olurdu: kimsenin sormadığı bir soru
yüzünden export'u durduran `deny`, ya da hiç bakmamış olmaktan ayırt edilemeyen
sessizlik. Kurala hiç uymayan dosya `UNKNOWN`'dır ve o da `CONFIRM`'e gider —
ama **ayrı sayılır**: "bilmediğimizi biliyoruz" ile "bakmadık" aynı sayı
olmamalı.

**Bunu GREEN'e çeviren bir bayrak yoktur** — böyle bir anahtar, kapının
kapatmak için var olduğu laundering yolunu yeniden açardı. Politikada
**catch-all `*` allow kuralı da yazılmaz**: o da bilinmeyeni sınıflandırmak
olur, depoya sonradan giren her dosya sessizce paylaşılabilir hâle gelir.

Bu deponun yürürlükteki politikası (`operator:berenrusatci`, 2026-08-10)
863 staged dosyanın **tamamını** sınıflandırıyor: 815 `PROJECT_OWNED` →
`allow`, 48 `VENDOR_DERIVED` → `confirm`, 0 eşleşmeyen. Yani kapı `CONFIRM`'de
kalıyor ve **kalması gerekiyor**: vendor-derived sınıf için hukuki paylaşım
otoritesi hâlâ tanımsız (K-1 açık). Politika GREEN üretmek için değil, bilineni
bilinmeyenden ayırmak için yazıldı.

## 6. Snapshot kimliği, makbuz ve atomik yayım

`MANIFEST.json` **zip'in içinde** (tüketicisi dış model),
`handoff_receipt.json` **setin dışında** (tüketicisi üretici/checker). Ayrımın
teknik nedeni basit: bir zip kendi SHA-256'sını kendi içinde tutarlı biçimde
taşıyamaz; makbuz arşivler oluştuktan sonra yazılır.

Kimlik: `repo_commit`, `branch`, `dirty`, dirty ise `git status --porcelain`
üzerinden `dirty_inventory_sha256`, export başlangıç/bitiş UTC damgaları,
`bundles_conf_sha256`, `export_tool_sha256`. Zip'ler yazıldıktan sonra her arşiv
**geri okunup** manifestteki hash'lerle karşılaştırılır: kontrol edilmemiş bir
manifest makbuz değil, vaattir.

Yayım atomiktir. Eski script yeni zip'leri kurmadan **önce** eskileri siliyordu;
ortada ölen bir koşu, bütün görünen yarım bir set bırakıyordu. Artık set yan
tarafta kurulur, tek bir rename ile `runs/<snapshot_id>` olur, ve `current`
ancak ondan sonra oraya bakar.

`GPTPRO_MAKER_ID` / `GPTPRO_CHECKER_ID` makbuza yazılır ve
`independence.separate_session` bunlardan türetilir. Semantik
`design-checker`'ın `checker_report.schema.json`'ından **yeniden kullanılır**,
yeniden icat edilmez.

## 6b. Türetilmiş görünüm — yalnız `EVIDENCE_INDEX`, yalnız `screen_only`

Kanıt JSON'u taşıyan her paket bir `EVIDENCE_INDEX.json` alır: her satır backing
dosyayı **ve onun sha256'sını** adlandırır, alanlar kaynak JSON'daki üst düzey
anahtarların **birebir kopyasıdır** (çıkarım yok; `verdict` anahtarı olmayan
dosyanın satırında verdict olmaz), ve `authority: screen_only`'dir. Bütçeyi aşan
bir değer sessizce kısaltılmaz — türü ve boyutuyla `__not_copied` diye
işaretlenir.

**`DESIGN_INDEX` bilerek üretilmiyor.** Sheet grafiği, refdes, net/node ve
diff-pair/netclass bağı gerçek KiCad S-expression yorumu ister; bir net hakkında
ince biçimde yanlış olan bir tasarım index'i, hiç index olmamasından kötüdür —
spec J-2'nin "türetilmiş görünüm yeni bir yalan söyleme yüzeyidir" uyarısı tam
olarak budur. Ham `.kicad_sch`/`.kicad_pcb` dosyaları tam paketlenir ve backing
store olarak kalır. Bu, kapatılmış değil **açık** bir maddedir.

## 7. Kapı davranışı

| Durum | Verdict | Yayım |
|---|---|---|
| required kaynak eksik | `NO_COVERAGE` | yok |
| bir paket `scanned = 0` | `NO_COVERAGE` | yok |
| `scanned + excluded != eligible` | `INVALID_COVERAGE` | yok |
| yasaklı dosya sınıfı stage'de | `RED` | yok |
| HARD credential eşleşmesi (`ALLOW_SECRETS=0`) | `RED` | yok |
| zip ↔ manifest hash uyuşmazlığı | `RED` | yok |
| disclosure politikası `deny` diyor | `RED` | yok |
| çalışma ağacı dirty | `CONFIRM` | **var**, upload yetkisiz |
| disclosure `UNKNOWN` (politika yok) | `CONFIRM` | **var**, upload yetkisiz |
| `ALLOW_SECRETS=1` kullanıldı | `CONFIRM` | **var**, upload yetkisiz |
| hepsi kapandı | `GREEN` | var, upload yetkili |
| `--recheck`: canlı HEAD/dirty ayrışmış | `STALE` | — |

Ayrım kasıtlı: `RED` sınıfı bir **hata**dır, set üretilmez. `CONFIRM` sınıfı bir
**karar**dır — paketler vardır, ama yükleme yetkisi operatörün açık kararına
kalır ve makbuz bunu `upload_authorized: false` diye kaydeder. Araç operatör
adına o kararı vermez, ama onun verilmiş gibi görünmesine de izin vermez.

**Dirty ağaç ve K-3.** Dirty bir ağaçtan handoff yapılabilir mi sorusu spec'te
açık. Muhafazakâr okuma alındı: koşu üretilir ama `GREEN` olmaz; drift
`dirty_inventory_sha256` ile sabitlenir, böylece tüketici en azından *aynı*
drift'e mi baktığını söyleyebilir.

## 8. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Neden |
|---|---|
| Bir dosyanın hukuken paylaşılabilir olduğu | Sözleşme sorusu; filesystem regex'i otorite değil. En iyi hâlde `CONFIRM`. |
| Modelin **hangi dosyayı gördüğü** | Manifest **offered corpus**'u kanıtlar, **consumed context**'i değil. Chat UI'ın retrieval davranışı ölçülemez. |
| Türetilmiş görünümlerin semantik doğruluğu | `derived_views[]` satırları backing hash'e bağlıdır ve `authority: screen_only`'dir; sign-off kanıtı değildir. |
| Snapshot'ın "yeterli" olduğu | Kapsam sözleşmesi `bundles.conf`'un kararıdır; araç onun *uygulandığını* ölçer, *doğru* olduğunu değil. |
| PCB hakkında herhangi bir şey | Otorite sınırı §1. |

## 9. Doğrulama

```bash
# tüm sözleşme testleri (fault-injection dahil)
pcb-agent/tools/run_tests.sh test_gptpro_export

# gerçek export + tazelik kontrolü
BUNDLES_CONF=$PWD/.gptpro/bundles.conf PROJECT_ROOT=$PWD \
  bash pcb-agent/skills/gptpro/scripts/export.sh
bash pcb-agent/skills/gptpro/scripts/export.sh --recheck
```

`test_gptpro_export.py` fault-injection ile şunları kanıtlar: eksik required
kaynak yayımı durdurur; `file:` ile eklenen yasaklı dosya `RED` verir; bozulmuş
aritmetik yakalanır; **üretilen kılavuzun paket listesi makbuzunkiyle aynıdır**
(elle tutulan `00-OKUNACAK.md` 8 zip'lik teslimatı 7 zip diye anlattığı için bu
bir regression fixture'dır).

## 10. Kabul kriterleri

- [ ] Her paket için `scanned + excluded == eligible` — dosya ve bayt.
- [ ] `missing_sources[]` boş; `optional_sources_absent[]` bilinçli.
- [ ] Her staged dosya için `sha256` + `repo_path` manifestte.
- [ ] `handoff_receipt.json` set dışında, her zip için `zip_sha256` +
      `manifest_sha256` ile.
- [ ] `00-OKUNACAK.md` üretilmiş; paket listesi makbuzunkiyle birebir.
- [ ] `credential_scan` kendi kapsam adıyla raporlanmış; "safe to upload"
      anlamına gelen bir cümle yok.
- [ ] `disclosure_gate` sonucu ve otoritesi makbuzda; otorite yoksa `CONFIRM`.
- [ ] Yayım `runs/<snapshot_id>` + `current`; yarım set görünür değil.

## İlgili

- [[../../researchGpt/gptpro-handoff/SKILL|gptpro-handoff]] — prompt yazma, rapor alma,
  `claim-audit`e devretme.
- [[../_shared/coverage-gate.md]] — kapalı verdict ve
  `evidence_origin` sözlükleri. Bu skill onları genişletmez.
- [[../claim-audit/SKILL|claim-audit]] — dönen raporun tek karar
  otoritesi.
