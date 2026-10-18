# PCB Ajanı — Skill Seti

24 skill (22 tasarım/denetim + 2 faz-dışı: handoff/dağıtım ve yürütme kapısı),
tek biçimli SKILL.md sözleşmesiyle. Bu dosya `MOCs/MOC-Skills.md`'nin yerini
alır (yeni adlarla güncellenmiştir).

> **Bu sayı elle kopyalanmaz.** Anlık değer:
> `ls pcb-agent/skills/*/SKILL.md | wc -l`. Bu satırdaki sayı yazıldığı gün
> (2026-08-12, **ölçüldü**: 24 — kopyalanmadı, komut koşuldu) doğrudur ve ertesi
> gün kaynak değildir — aynı sayacın iki dosyada iki farklı değer taşıdığı
> ölçülmüş bir vaka için
> [`../docs/tool-inventory.md`](../docs/tool-inventory.md) §Sayaç drift'i.
>
> **Proje-seviyesi skill'ler bu sayıya dahil değildir.** Çok-kartlı sistem
> sözleşmesinin sahibi
> `sistem-arayuz-sozlesmesi`
> bilerek generic sete alınmadı: araçları sabit kamera topolojisi ve
> RK3588'e özgü mantık taşıyor
> (bu depoda yer almaz; kaynak projeye özgüdür).

Ortak katman: [`_shared/`](_shared/) · Araç envanteri:
[`../docs/tool-inventory.md`](../docs/tool-inventory.md) · Konsolidasyon kararları:
[`../docs/CONSOLIDATION-NOTES.md`](../docs/CONSOLIDATION-NOTES.md)

---

## Yürütme ve karar

| Skill | Ne yapar | Faz |
|---|---|---|
| [decompose-pcb-decisions](decompose-pcb-decisions/SKILL.md) | Atomik karar birimleri + hash'li minimum context pack | 0 |
| [pcb-orchestrator](pcb-orchestrator/SKILL.md) | DAG, dosya sahipliği, kalite kapıları, gate receipt | hepsi |
| [claim-audit](claim-audit/SKILL.md) | Dış inceleme/iddia denetimi: önce ölç, sonra düzelt | faz-dışı |
| [design-checker](design-checker/SKILL.md) | Maker/Checker ayrımı: üreticinin "bitti" dediğini bağımsız denetler; fab çıktısından önceki kapı | 4a |
| [paralel-yurutme](paralel-yurutme/SKILL.md) | Fan-out kâr kapısı (dört test) + yazar-ayrıklığı kanıtı + uzun koşu yürütme zarfı; `FANOUT_APPROVED` / `INLINE_CHEAPER` / `FANOUT_UNSAFE` | faz-dışı |

## Tasarım adımları

| Skill | Ne yapar | Faz | Board yazar |
|---|---|---|---|
| [guc-isil](guc-isil/SKILL.md) | Rail bütçesi, junction sıcaklığı, LDO/SMPS, iz/via boyutlandırma | 1 | ➖ |
| [mekanik-dxf](mekanik-dxf/SKILL.md) | Outline, montaj deliği, 3D keepout hacmi, datum | 1 | ➖ |
| [bom-lifecycle](bom-lifecycle/SKILL.md) | Stok, lifecycle, pin-compatible alternatif, provenance | 1 | ➖ |
| [ecad-varlik-kalifikasyonu](ecad-varlik-kalifikasyonu/SKILL.md) | Seçilmiş MPN'in symbol+footprint'i authoritative pin/package tanımına **semantik** olarak uyuyor mu; asset serbest bırakma kapısı | 1 | ➖ |
| [konnektor](konnektor/SKILL.md) | Seçim, mating cycle, akım derating, FPC landing, shell topraklama | 1 + 3 | ➖ |
| [arayuz-eslesme-kapisi](arayuz-eslesme-kapisi/SKILL.md) | Seçilmiş **iki** parça birbirine takılır mı ve pinler uçtan uca örtüşüyor mu; eksik halka bir ihlaldir, sessizlik değil. `sematik-erc`'yi bloklar | 1 (+3) | ➖ |
| [empedans-stackup](empedans-stackup/SKILL.md) | Katman yapısı, W/S çözümü, referans düzlemi, coupon | 2 | ➖ |
| [sematik-erc](sematik-erc/SKILL.md) | Gerçek wire, S-Expr enjeksiyon, ERC auto-loop, netlist kanıtı | 2 | ✅ |
| [dft-testpoints](dft-testpoints/SKILL.md) | Test noktaları, sınıf bazlı kapsama, bring-up checklist | 2 + 4 | ✅ |
| [dfm](dfm/SKILL.md) | DRC kural enjeksiyonu, yerleşim, teardrop, via-in-pad, MLCC flex crack | 3 + 4 | ✅ |
| [highspeed-routing](highspeed-routing/SKILL.md) | Escape geometrisi + length-match + kuplaj + referans sürekliliği | 3 | ✅ |
| [otonom-routing](otonom-routing/SKILL.md) | Kritik netler kilitlendikten sonra kalan netlerin sınırlı (bütçeli) otomatik yönlendirmesi; router çıktısı verdict değildir | 3 | ✅ |
| [crosstalk-thinning](crosstalk-thinning/SKILL.md) | Paralel iz NEXT taraması, güvenli inceltme (MCP) | 3 | ➖ |
| [emc-esd](emc-esd/SKILL.md) | EMC hedefi, CM/kablo, dönüş yolu, stitching, ESD zinciri | 2 + 3 | ➖ |
| [cpl-assembly](cpl-assembly/SKILL.md) | Gerber/BOM/CPL, rotasyon, oryantasyon, DNP parity, panel | 4 | ➖ |

## Fiziksel kapanış (Faz 5 — kart geldikten sonra)

> Faz 0→4 bir **design-to-manufacturing-release** DAG'ıdır; 4c'nin çıktısı bir
> dosya paketidir, monte edilmiş kart değil. L3 ("kart geldikten sonra") o
> DAG'ın dışında kalıyordu; aşağıdaki iki skill onu kapatır ve **birleştirilmez**
> — as-built release ≠ elektriksel karakterizasyon.

| Skill | Ne yapar | Faz | Board yazar |
|---|---|---|---|
| [ilk-urun-montaj-kabulu](ilk-urun-montaj-kabulu/SKILL.md) | Released paketten üretilen ilk kartın as-built konformansı; seri montajı açar ya da bloke eder | 5A | ➖ |
| [l3-bringup-karakterizasyon](l3-bringup-karakterizasyon/SKILL.md) | Fiziksel ölçüm kaydının belirli board rev/seri ve D-kuralını kapatmaya yeterli olup olmadığı; deponun tek `PHYSICAL_MEASUREMENT` üreticisi | 5B | ➖ |

## Denetim

| Skill | Ne yapar |
|---|---|
| [dogrulama-matrisi](dogrulama-matrisi/SKILL.md) | Kural → seviye (A/B/C/D) → araç → döngü (L0..L3) eşlemesi; "kanıtlanamaz" listesi |

## Handoff / dağıtım kapısı

| Skill | Ne yapar | Faz |
|---|---|---|
| [gptpro](gptpro/SKILL.md) | Dış inceleme korpusunu paketler: paket başına dosya/bayt kapsam aritmetiği, dışlama envanteri, dosya başına SHA-256, paket içi `MANIFEST.json`, set dışı `handoff_receipt.json`, atomik run-id yayımı, credential taramasından **ayrı** disclosure kapısı | faz-dışı |

Neden denetim katmanında: paketleme bir gürültü temizliği değil, bir **assurance
boundary**'dir. Bir dosyayı paketten düşürmek, o dosya hakkındaki her dış
bulguyu önceden imkânsız kılar. Otoritesi tek bir hükümle sınırlıdır — *bu
snapshot şu declared scope'u şu hash'lerle taşıyor* — ve kartın doğruluğu
hakkında hiçbir şey söylemez.

Kendi `scripts/` dizinini taşıyan tek skill budur; `tool_registry.json` hem
`export.sh`'i hem motoru locator + SHA-256 ile kayda alır, yani script'lerin
`tools/scripts/` altına dağılması gerekmez.

Workflow eşi `gptpro-handoff` (bu depoda yok) bilinçli
olarak kanonik depo skill'i **değildir** ve `skills/` altına alınmadı: kendi
başına ölçülebilir PCB kanıtı üretmez ve dönen raporun verdict'ini
[claim-audit](claim-audit/SKILL.md)'e devreder.

---

## Ortak katman (`_shared/`)

| Dosya | İçerik |
|---|---|
| [coverage-gate.md](_shared/coverage-gate.md) | `eligible/scanned/excluded/violations` kapısı, `evidence_origin` ve `assurance_loop` sözlükleri, verdict sözlüğü |
| [glossary.md](_shared/glossary.md) | Adı çakışan büyüklükler: `f_knee_edge` ≠ `f_nyquist`, `parallelism_coverage` ≠ kuplaj, HBM ≠ IEC 61000-4-2, kanonik artifact adları |
| [rule-conflicts.md](_shared/rule-conflicts.md) | İki doğru kuralın çeliştiği yerler ve hangisinin kazandığı |

---

## SKILL.md sözleşmesi

Her skill aynı frontmatter ve bölüm iskeletini taşır:

```yaml
name / description / version / last-reviewed
phase / assurance_loop / evidence_origin / signoff
inputs[] / outputs[]   # her biri artifact + schema
writes_board           # orkestratör kilidi bunu okur
tools[]                # config/tool_registry.json ile eşleşir
```

Bölümler: **0.** Ne zaman çağrılır → **1.** Girdi/çıktı sözleşmesi →
**2.** Adımlar → **3.** Nüanslar → **4.** Bilinen sınırlar (neyi
KANITLAYAMAZ) → **5.** Doğrulama (çalıştırılabilir + fault-injection) →
**6.** Kabul kriterleri (`- [ ]`) → **İlgili**.

**Tek sahiplilik kuralı:** bir kural tam olarak bir skill'e aittir; diğerleri
wikilink ile atıf yapar, içeriği tekrar etmez. Kanonik sahipler:

| Kural | Sahibi |
|---|---|
| Coverage kapısı | `_shared/coverage-gate.md` |
| MLCC DC-bias | guc-isil §3b |
| MSL / floor life | bom-lifecycle §3a |
| MLCC flex crack | dfm §4f |
| Teardrop kararı | dfm §3 |
| EPAD pasta | dfm §4c |
| DRC kural enjeksiyonu | dfm §2 |
| Maske barajı hesabı | highspeed-routing §2a |
| `parallelism_coverage` | highspeed-routing §4 |
| Teardrop API sınırı | highspeed-routing §5c |
| Referans düzlemi / `100 nF` | empedans-stackup §Referans düzlemi |
| Cam dokuma skew | empedans-stackup §3e |
| Stitching via (iki kural) | emc-esd §4 |
| ESD zinciri | emc-esd §7 |
| `--schematic-parity` uyarısı | sematik-erc §4 |
| 3D keepout şeması | mekanik-dxf |
| DNP parity | cpl-assembly §2.3 |
| Ölçüm hatası sınıfları | dogrulama-matrisi/references/measurement-error-classes.md |
| Board yazımı kilidi | pcb-orchestrator/references/board-write-lock.md |
| Fan-out kârlılık kapısı (ölçülmemiş hızlanma kanıt değildir) | paralel-yurutme §2 |
| Fan-out öncesi yazar-ayrıklığı **kanıtı** (limitin sahibi board-write-lock) | paralel-yurutme §3 |
| Uzun koşu yürütme zarfı (timeout, çıktı kesme, exit-1 tuzağı) | paralel-yurutme/references/execution-envelope.md |
| `fanout_coverage` ≠ `parallelism_coverage` | `_shared/glossary.md` §8 |
| Router çıktısı ≠ verdict | otonom-routing |
| `assurance_loop` sahiplik taşımaz (participates-in ≠ produces-evidence-for) | `_shared/coverage-gate.md` |
| Verdict isim alanı (`gate.verdict` ≠ domain/workflow state) | `_shared/coverage-gate.md` |
| Symbol/footprint ↔ authoritative pin/package semantiği (küme eşitliği) | ecad-varlik-kalifikasyonu |
| Parça↔parça fiziksel takılabilirlik + uçtan uca pin örtüşmesi (**sıra**/permütasyon) | arayuz-eslesme-kapisi §3b |
| Ara-bağlantı kenar evreni (`eligible` sözleşmeden türer, beyandan değil) | arayuz-eslesme-kapisi §3a |
| Kablo kontak-yüzü normalizasyonu (satıcı harfi anahtar DEĞİL) | arayuz-eslesme-kapisi §3c |
| Aynalanma / `contact_face` iki-taraflı ilişkisi | arayuz-eslesme-kapisi §3i |
| As-built kabul ve seri üretim release'i | ilk-urun-montaj-kabulu |
| Fiziksel kanıt yeterliliği / L3 kapanışı (limit değil!) | l3-bringup-karakterizasyon |
| Release-health (üretilmiş dosya + sayaç drift'i) | pcb-orchestrator §3 |
| Cross-board contract + HW/SW binding | `system-planning/skills/sistem-arayuz-sozlesmesi` (proje seviyesi) |

---

## Skill'e terfi kuralı

Araştırmacı raporu tek başına skill değişikliği **değildir**. Yalnız `KABUL`
kararı alan, birincil kaynağı ve yeniden üretilebilir doğrulaması bulunan
maddeler ilgili skill'e **kısa, imperatif bir kural** olarak eklenir. Ayrıntı
araştırma notunda kalır. Seviye ataması (A/B/C/D) `dogrulama-matrisi`'nde
yapılır. Kapı:

```bash
python3 tools/scripts/research_promotion_gate.py --vault . --json promotion_report.json
```

---

## Doğrulama

```bash
python3 -m unittest discover -s tools/tests -t .        # tüm regresyon
python3 tools/scripts/sync_tool_registry.py --check     # registry ağaçla uyuşuyor mu
python3 tools/scripts/sync_tool_registry.py --write     # hash'leri tazele
```

CLI sözleşmesi: **`--json` her zaman YAZAR**; girdiyi JSON'dan okumak için
`--input-json`; kanonik doğrulama bayrağı `--self-test`.
