---
name: design-checker
description: Maker/Checker ayrımı — üretici skill'lerin "bitti, 0 hata" dediği tasarımı bağımsız bir denetmen rolüyle yeniden okur. Maker'ın raporunu veri olarak alır ama sonuçlarına güvenmez; ham netlist, DRC/ERC JSON ve BOM üzerinden kendi başına ölçer. Faz 4a kapısıdır: bu skill GREEN demeden fab çıktısı üretilmez. Tasarımı DÜZELTMEZ, yalnız bulgu raporu yazar.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 4a — bağımsız denetim (fab çıktısından önce)"
assurance_loop: [L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: board, schema: null, required: true}
  - {artifact: schematic, schema: null, required: false}
  - {artifact: drc-gate-report.json, schema: ../../tools/schemas/drc_gate_report.schema.json, required: true}
  - {artifact: bom.csv, schema: null, required: true}
  - {artifact: "maker raporları (TEST/*.md)", schema: null, required: false}
outputs:
  - {artifact: checker_report.json, schema: references/checker_report.schema.json}
  - {artifact: acik_maddeler.md, schema: null}
writes_board: false
tools: [drc-gate, independent-verify, gate-receipt, dfm-emc-check, mask-dam, via-stub, collision-radar, kicad-cli, gerber-dfm, reference-plane, decoupling-coverage, current-density, cross-section]
---

# SKILL: Bağımsız Denetim (Maker / Checker)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Ölçüm tuzakları: [[../dogrulama-matrisi/references/measurement-error-classes.md]]

## 0. Ne zaman çağrılır

- Üretici skill'ler ([[../sematik-erc/SKILL|sematik-erc]],
  [[../dfm/SKILL|dfm]], [[../highspeed-routing/SKILL|highspeed-routing]])
  DRC/ERC'yi temiz raporladıktan **sonra**, fab çıktısı üretilmeden **önce**.
- `pcb-agent` akışında: Faz 4 birleşik kapısına girmeden hemen önce.
  `tools/scripts/production/outputs.py` bu skill GREEN vermeden çağrılmaz.

**Bu skill ile [[../claim-audit/SKILL|claim-audit]] farkı** — ikisi de "rapora
güvenme, ölç" der ama tetikleyicileri ve konumları ayrıdır:

| | design-checker | claim-audit |
|---|---|---|
| Tetikleyici | faz sınırı (her tasarımda zorunlu) | dış iddia (ad hoc) |
| DAG'daki yeri | Faz 4a, akışın içinde | faz-dışı |
| Girdi | Maker'ın kendi çıktısı | başkasının iddia listesi |
| Amaç | kör nokta avı | iddia doğrulama |

Denetim yöntemi ortaktır ve **tekrarlanmaz**: numaralı evidence klasörü, yan
etki ölçümü, bisect ve push öncesi son tarama için
[[../claim-audit/SKILL|claim-audit]] §2–§8'i uygula.

> **Bağımsızlık sınırı — dürüst olalım.** Aynı ajan hem üretip hem denetlerse
> bu *rol* ayrımıdır, *kişi* ayrımı değil; aynı varsayımı iki kez yapma riski
> tamamen ortadan kalkmaz. Bu yüzden çıktı `evidence_origin=TOOL_REPORT`'tur:
> denetmenin kanısı değil, **yeniden koşulmuş aracın çıktısı** kanıttır. Gerçek
> bağımsızlık isteniyorsa denetimi ayrı bir oturumda/ajanda koştur ve bunu
> `checker_report.json`'daki `independence` alanına yaz.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Uydurulursa |
|---|---|---|
| `drc.violations[]` | `drc_gate.py` **yeniden koşulur** | Maker'ın JSON'unu kopyalamak denetim değildir |
| `erc.violations[]` | `kicad-cli sch erc --exit-code-violations --severity-all` | bayrak yoksa sonuç geçersiz |
| `bom[].mpn/footprint/rating` | ham BOM dosyası | eksik alan = bulgu, varsayılan atama yok |
| `netlist` | `kicad-cli sch export netlist` | şematikteki çizili tel bağlantı kanıtı değildir |

**Zorunlu:** her kontrol kendi `scanned` sayısını yayınlar. Maker'ın raporunu
okumak bir kontrolü `scanned` yapmaz — yalnız aracı yeniden koşmak yapar.
`scanned=0` → `NO_COVERAGE`, asla GREEN değil.

## 2. Adımlar

1. **Ham veriyi kendin oku.** Maker'ın `TEST/*.md` özetini *girdi* say, *sonuç*
   sayma. Netlist'i, `.kicad_pcb`'yi, BOM'u kendin aç; her güç ve kritik neti
   kendin say.
2. **DRC/ERC'yi yeniden koştur** — `tools/scripts/drc_gate.py <board>`.
   Maker'ın kaydettiği raporu kabul etme: kartın o rapordan sonra değişmiş
   olması en sık rastlanan sessiz hata kaynağıdır (hash'i karşılaştır).
3. **`warning` seviyesinde bırakılmışları tara.** "error değil" bir gerekçe
   değildir. `--severity-all` olmadan alınmış her rapor `NO_COVERAGE`'dır.
4. **Bağımsız ikinci doğrulayıcıyı koştur** —
   `tools/scripts/independent_verify.py`. DRC'den bağımsız yoldan aynı sonuca
   varılıyor mu?
5. **Şeytanın avukatlığı.** Aşağıdaki soruları kendin üret ve devre üzerinde
   tek tek doğrula; Maker'ın raporunda cevabı yoksa bu **başlı başına bir
   bulgudur**:
   - I²C pull-up'ı var mı ve değeri **hesapla** doğrulanmış mı? Sabit 4.7k bir
     gerekçe değildir → [[../sematik-erc/SKILL|sematik-erc]] §I²C.
   - Kapasitör gerilim sınıfı worst-case altında mı — DC-bias derating dahil?
     → [[../guc-isil/SKILL|guc-isil]] §MLCC DC-bias.
   - Regülatör EN / power-sequencing datasheet sırasına uyuyor mu?
   - Aynı IC'nin birden çok örneği varsa NC pinleri kopyala-yapıştır ile
     yanlış bağlanmış olabilir mi?
6. **Yerleşim doğruluğunu görselden değil JSON'dan al** —
   `tools/scripts/board/collision_radar.py`. Render edilmiş görüntü niteliksel
   bir ipucudur, ölçüm değildir.
7. **Bulgu raporunu yaz** (§6) ve verdikti [[../_shared/coverage-gate.md]]
   sözlüğüne eşle.

## 3. Nüanslar

**(a) Maker'ın raporu ile kartın hash'i uyuşmalı.** Rapor alındıktan sonra
karta dokunulmuşsa rapor `STALE`'dir. Denetim, raporun değil **kartın**
denetimidir.

**(b) "Temiz DRC" bir kapsam iddiası değildir.** DRC yalnız kendisine
tanımlanmış kuralları görür. Enjekte edilen `.kicad_dru` kuralının gerçekten
koştuğu kanıtlanmadıysa (fault-injection), "0 ihlal" bilgi taşımaz —
[[../dfm/SKILL|dfm]] §DRC kural enjeksiyonu ve
[[../_shared/rule-conflicts.md]] §8.

**(c) Düzeltme yasağı bir tarafsızlık aracıdır.** Bu skill kusuru bulur ve
tarif eder; düzeltmeyi üretici skill yapar. Denetmen düzeltirse bir sonraki
turda kendi düzeltmesini denetlemiş olur ve rol ayrımı çöker.

**(d) Bulgu şiddeti verdikti belirler, sayısı değil.** Tek bir CRITICAL, otuz
LOW'dan ağırdır.

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

- **Fiziksel davranış.** Tüm çıktısı L1'dir; EMC uygunluğu, gerçek empedans ve
  termal davranış L3 (fiziksel döngü) ister.
- **Kapsanmayan kusur sınıfları.** Denetim yalnız koşturduğu araçların
  gördüğünü görür. Hiçbir aracın taramadığı bir sınıf varsa bu `NO_COVERAGE`
  olarak **yazılır**, sessizce PASS'a dönüşmez.
- **Kendi kör noktası.** Üretici ile denetleyici aynı model/oturumsa ortak
  varsayım hatası ölçülemez. Bkz. §0 bağımsızlık kutusu.

## 5. Doğrulama — çalıştırılabilir

```bash
python3 tools/scripts/drc_gate.py <board.kicad_pcb> --json evidence/drc.json
python3 tools/scripts/independent_verify.py <board.kicad_pcb> \
    --contract tools/schemas/independent_verify_contract.schema.json \
    --json evidence/independent.json
python3 tools/scripts/board/collision_radar.py <board.kicad_pcb> --json evidence/collision.json
python3 tools/scripts/gate_receipt.py evidence/checker_receipt.json
```

**Fault-injection (denetimin boş olmadığının kanıtı):** karta bilinen bir kusur
enjekte et (ör. bir pad'i clearance altına kaydır), denetimi tekrar koş.
Verdikt `RED`'e dönmüyorsa denetim çalışmıyordur — kusuru değil denetimi düzelt.

## 6. Bulgu raporu ve verdikt eşlemesi

`checker_report.json` şeması: [[references/checker_report.schema.json]].
İnsan okuru için `TEST/checker_raporu.md` de yazılır.

Kaynak skill `PASS / CONDITIONAL PASS / FAIL` sözlüğü kullanıyordu. Bu paket
**kapalı bir verdikt sözlüğüne** sahiptir ([[../_shared/coverage-gate.md]]) ve
paralel bir sözlük açmak yasaktır. Eşleme:

| Denetmen sonucu | Kanonik verdikt | Sonuç |
|---|---|---|
| Kritik/yüksek bulgu yok, tüm kontroller koştu | `GREEN` | Faz 4 birleşik kapısına geçilir |
| Yalnız düşük şiddetli bulgu var | `CONFIRM` | İnsan onayı ister; sessizce geçilmez |
| ≥1 CRITICAL/HIGH bulgu | `RED` | Üretici faza geri dönülür, fab çıktısı **üretilmez** |
| Bir kontrol hiç koşmadı / `scanned=0` | `NO_COVERAGE` | GREEN değildir; eksik kapsam yazılır |
| Maker raporu ile kart hash'i uyuşmuyor | `STALE` | Denetim tekrarlanır |

## 7. Kabul kriterleri

- [ ] DRC ve ERC bu denetim içinde **yeniden koşuldu**; Maker'ın raporu
      kopyalanmadı (argv ve çıkış kodu makbuzda)
- [ ] Her rapor `--exit-code-violations` **ve** `--severity-all` ile alındı
- [ ] `warning` seviyesindeki maddeler tek tek değerlendirildi ve gerekçesi yazıldı
- [ ] Bağımsız ikinci doğrulayıcı koştu ve sonucu DRC ile karşılaştırıldı
- [ ] Yerleşim doğruluğu JSON çarpışma radarından alındı, görselden değil
- [ ] Board hash'i, denetime giren raporların hash'iyle eşleşiyor
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`
- [ ] Fault-injection koşuldu ve denetim `RED` üretti
- [ ] Verdikt §6 tablosundaki kanonik sözlükten; paralel sözlük yok
- [ ] `gate_receipt.json` GREEN (`python3 tools/scripts/gate_receipt.py <dosya>` exit 0)

## İlgili

[[../claim-audit/SKILL|claim-audit]] (yöntem sahibi) ·
[[../pcb-orchestrator/SKILL|pcb-orchestrator]] (faz sırası) ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] (seviye taksonomisi) ·
[[../dfm/SKILL|dfm]] (DRC kural enjeksiyonu)

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`gerber-dfm`** — Gerçek export edilmiş RS-274X gerber'lardan DFM kanıtı — DRC'den **bağımsız** ikinci okuma.
- **`reference-plane`** — İz segmentinin referans düzlem sürekliliği; split/kesik düzlem dönüş yolunu kırar.
- **`decoupling-coverage`** — IC güç pinlerini **board'dan keşfeder** ve her biri için en yakın uygun kapasitörün mesafesini ölçer — unutulan pin sessizce kapsam dışı kalmaz.
- **`current-density`** — Bakır döküldükten sonra DC akım yoğunluğu (direnç-ağı çözücü) + ısı haritası; dar boyun bu haritada çıkar.
- **`cross-section`** — Kartın bir bölgesini ajanın **görebileceği** PNG'ye basar; bir ölçüm tartışmalı olduğunda bakılacak şey.
