---
name: claim-audit
description: Dış inceleme veya "şu kartta şu hatalar var" iddiası geldiğinde uygulanacak denetim akışı — önce ölç sonra düzelt, DOĞRU/YANLIŞ/DOĞRULANAMAZ verdict sözlüğü, numaralı evidence klasörü, yan etki ölçümü, bisect ve push öncesi son tarama.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz-dışı — dış inceleme/denetim modu"
assurance_loop: [L0, L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: iddia listesi, schema: null, required: true}
  - {artifact: board, schema: null, required: true}
  - {artifact: netlist.xml, schema: null, required: false}
outputs:
  - {artifact: "evidence/00_RAPOR.md", schema: null}
  - {artifact: "evidence/NN_*.json", schema: null}
  - {artifact: acik_maddeler.md, schema: null}
writes_board: false
tools: [gate-receipt, dfm-emc-check, mask-dam, stitch-density, via-stub, kicad-cli]
---

# SKILL: İddia Denetimi (dış inceleme geldiğinde)

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Ölçüm tuzakları: [[../dogrulama-matrisi/references/measurement-error-classes.md]]

> Bu skill [[../pcb-orchestrator/SKILL|pcb-orchestrator]]'den **ayrıldı**
> (konsolidasyon S1). DAG akışının parçası değildir: tetikleyicisi bir dış
> iddiadır, bir faz sınırı değil. Orkestratör ileriye doğru üretir; bu skill
> mevcut bir karta yöneltilmiş iddiaları geriye doğru denetler.

**Ölçülmüş sonuç (usb-hs-breakout):** 20 iddianın **3'ü yanlış** çıktı ve
denetim sırasında **kimsenin söylemediği 7 yeni kusur** ortaya çıktı.

## 0. Ne zaman çağrılır

- Biri "şu kartta şu hatalar var" dediğinde (insan review, başka ajan raporu,
  fab DFM raporu, otomatik tarayıcı).
- Bir kod yorumu / doküman satırı bir metrik iddia ediyorsa ve bu iddia bir
  karara girecekse.
- Push öncesi son tarama için (§8).
- **Bu skill board'a yazmaz.** Düzeltme gerekiyorsa ilgili adım skill'i ve
  transaction kuralları devreye girer
  ([[../pcb-orchestrator/references/board-write-lock.md]]).

## 1. Önce ÖLÇ, sonra düzelt — ve iddiaya göre değil dosyaya göre ölç

Hiçbir iddiayı doğru varsayma; hiçbirini de peşinen reddetme. Her iddia için
kaynak dosyadan (`.kicad_pcb` / `.kicad_sch` / netlist) **yeniden ölçüm** yap.
**Kod yorumu, doküman satırı ve başka ajanın raporu kanıt değildir.**

> Ölçülmüş örnek: "C1 U1 VBUS pinine ~1.4 mm" bir **kod yorumuydu**; ölçülen
> değer **4.307 mm**. Kural 3× ihlal ediliyordu ve bunu kimse fark etmemişti.

## 2. `evidence/` klasörü — numaralı, yeniden çalıştırılabilir

Her ölçümü dosyaya yaz, sırayla numarala (`00_RAPOR.md`, `01_...`, `02_...`).
Ölçüm **araçları da** aynı klasörde dursun ki üçüncü bir kişi sayıyı yeniden
üretebilsin. Değişiklik öncesi kart/şematiği `*.orig` olarak sakla —
**`.bak` yetmez, script'ler onu ezer.**

Her ölçüm artifact'ında `metric_definition`, units, center-vs-edge semantiği,
region, sampling step ve geometry model **zorunlu** olsun. Arc'ı chord ölçen
araç arc-aware sonucu temsil edemez → `INVALID_METRIC`.

## 3. Verdict sözlüğü: DOĞRU / YANLIŞ / DOĞRULANAMAZ

Üçüncüsü şart. **"Doğrulayamadım" ≠ "yanlış".** İki alt-tür:

- **metrik tanımsız** → sayı yeniden üretilemez (ör. "%75 kuple" — hangi
  toleransta? hangi örneklemede?). Aracı yaz, tanımı sabitle, yeniden ölç.
  Kanonik metrik adı ve zorunlu alanlar: [[../_shared/glossary.md]] §2.
- **atıf yapılan dosya yok** → `grep`/`find` ile ara, bulunamazsa
  **DOĞRULANAMADI** yaz; var sayma.

## 4. Yanlış çıkan iddiada "neden yanlış okundu?"yu sor

Bir iddiayı çürütmek işin **yarısıdır**. Dikkatli bir insan neden yanıldı? Cevap
genelde **gerçek bir okunabilirlik kusurudur** ve asıl düzeltilecek şey odur.

> Ölçülmüş örnek: "ESD'de D+/D− çaprazlanmış" iddiası netlist'le çürütüldü. Ama
> sebep gerçekti: SOT-23-6'da `I/O1`'in iki pini paketin **zıt köşelerinde**,
> şema görsel olarak çapraz görünüyor. Çözüm netlist'i değiştirmek değil,
> şemaya **açıklama bloğu** koymaktı.

## 5. Scratch'te dene → doğrula → sonra asıl dosyaya

Board/şematik yazan hiçbir script'i ilk seferde canlı dosyaya koşturma.
`<scratch>/test.kicad_pcb`'ye yaz, DRC/ERC/netlist'ten geçir, **ancak sonra**
asıl dosyaya uygula.

> Alt-ajanın verdiği koordinatlar neredeyse her zaman doğrulanmamış tahmindir:
> bir alt-ajanın TP koordinatları çalıştırıldığında **4 DRC ihlali** verdi
> (courtyard çakışması + 2 ipek + kenar).

## 6. Düzeltmenin YAN ETKİSİNİ de ölç — "değişmemesi gereken" listesi

Her düzeltmeden sonra sadece hedef metriği değil, **dokunulmaması gereken**
metrikleri de ölç ve raporda "değişmedi" diye göster. Bu, regresyon olmadığının
**tek kanıtıdır**.

> Ölçülmüş örnek (usb-hs-breakout): VBUS/via/TP düzeltmeleri sonrası D+/D−
> uzunluk (30.987 mm), skew (0.000 mm) ve `parallelism_coverage` (%64.1 @ tol
> 0.70 mm) **birebir aynı** kaldı → çifte dokunulmamış.

## 7. Kontrolün gerçekten koştuğunu kanıtla

**"0 sorun" ile "kontrol çalışmadı" raporda aynı görünür.**

- Bayrağı unutma: `--schematic-parity` olmadan parity listesi **hep boş** gelir;
  `--severity-all` olmadan koca kategoriler gizlenir; `--exit-code-violations`
  olmadan ihlal exit code'a yansımaz.
  Kanonik uyarı: [[../sematik-erc/SKILL|sematik-erc]] §4.
- **Fault-injection:** bir teli 1.27 mm kaydır (pin ucundan), kontrol
  **KIRILMALI**. Kırılmıyorsa kontrol boştur.
- Pozitif fixture yanında **negatif** fault-injection da koş: doğru yapı PASS,
  katman/zone/geometry bozulmuş yapı FAIL vermeli.
- Kendi düzeltmen bir kontrolü **ilk kez** tetikliyorsa (17 parity + 186 silk
  ihlali gibi) bu **iyi haberdir**: kontrol canlı demektir.

## 8. Mesajsız hatada BISECT

`Failed to load schematic` gibi satır/sebep vermeyen hatalarda tahmin yürütme:
değişiklikleri tek tek ayrı dosyalara uygula, her birini yükle, patlayanı bul.
Kontrol koşusu olarak dosyayı **hiç değiştirmeden** yaz–oku yap (encoding/locale
kaynaklı bozulmayı eler).

## 9. Düzeltilemeyeni "düzeltildi" diye yazma

API yoksa / geometri elvermiyorsa: **açık madde** olarak yaz, gerekçesini
ölçüyle belgele, insan adımını tarif et. Fonksiyon `API_YOK: <sebep>` döndürsün.
Kapatılamayan her madde README + rapor + strateji dokümanının **üçünde** de
görünsün.

## 10. Push öncesi son tarama (kolay atlanan 4 şey)

1. **Takip edilen bayat çıktılar**: `_netlist.net`, `*_erc.json` gibi git'te olan
   üretilmiş dosyalar — yeniden üret, yoksa yanlış bilgi push'larsın.
2. **Downstream config'ler**: değişikliği sessizce geri alacak dosyalar
   (hedef netlist JSON'u, üretici script). Güncelle **veya** dosyanın içine
   "bunu çalıştırırsan şu kaybolur" uyarısı koy.
3. **Kontrol listelerinin durumu**: `- [ ]` kutuları ölçülen gerçeğe göre
   işaretle; sağlanmayanı `**AÇIK:**` diye ayır.
4. **KiCad kapalı mı** + `.lck` yok + dosya mtime'ları hâlâ senin yazdığın an.

## 11. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| İddianın **niyeti** | — | Yalnız ölçülebilir içeriği denetlenir |
| C/D seviyesi iddialar | C/D | Ölçüm/lab gerekir; `acik_maddeler.md`'ye düşer |
| Kapsam dışı bırakılan iddia | — | `excluded` + gerekçe zorunlu; sessizce atlanamaz |
| Düzeltmenin doğruluğu | — | Bu skill denetler, düzeltmez; düzeltme adım skill'inin işi |

## 12. Doğrulama

```bash
# iddia başına yeniden ölçüm  (--json YAZAR)
python3 tools/scripts/board/dfm_emc_check.py board.kicad_pcb --json evidence/01_dfm.json
python3 tools/scripts/board/mask_dam.py      board.kicad_pcb --json evidence/02_mask.json
python3 tools/scripts/board/via_stub.py      board.kicad_pcb --json evidence/03_stub.json

# bayraklar zorunlu — yoksa "0 sorun" ile "kontrol koşmadı" ayırt edilemez
kicad-cli sch erc --format json --severity-error --severity-warning \
  --exit-code-violations -o evidence/04_erc.json X.kicad_sch
kicad-cli pcb drc --schematic-parity --format json --severity-all \
  --exit-code-violations -o evidence/05_drc.json X.kicad_pcb

python3 tools/scripts/gate_receipt.py evidence/00_gate_receipt.json
```

## 13. Kabul kriterleri

- [ ] Her iddia için kaynak dosyadan **yeniden ölçüm** yapıldı; kod yorumu /
      doküman satırı / başka ajan raporu kanıt sayılmadı.
- [ ] Her iddia üç verdict'ten birini aldı: **DOĞRU / YANLIŞ / DOĞRULANAMAZ**;
      "doğrulanamadı" hiçbir yerde "yanlış"a çevrilmedi.
- [ ] `evidence/` numaralı, ölçüm araçlarını içeriyor ve üçüncü kişi sayıyı
      yeniden üretebiliyor; `*.orig` saklandı.
- [ ] Her ölçüm artifact'ı `metric_definition` + units + distance semantics +
      region + sampling step + geometry model taşıyor.
- [ ] Yanlış çıkan her iddiada "neden yanlış okundu?" sorusu cevaplandı ve
      varsa okunabilirlik kusuru ayrı madde olarak açıldı.
- [ ] Board yazan hiçbir script canlı dosyada ilk kez koşmadı (scratch → doğrula
      → uygula).
- [ ] Her düzeltme sonrası **değişmemesi gereken** metrikler ölçüldü ve
      "değişmedi" diye raporlandı.
- [ ] Tüm ERC/DRC koşuları `--exit-code-violations` (+ parity için
      `--schematic-parity --severity-all`) ile yapıldı.
- [ ] Fault-injection koşuldu (pozitif + negatif); kontrolün boş olmadığı kanıtlandı.
- [ ] Kapatılamayan her madde `API_YOK: <sebep>` / **AÇIK** olarak yazıldı,
      "yapıldı" diye geçilmedi.
- [ ] Push öncesi dört madde tarandı (bayat çıktılar, downstream config'ler,
      checklist durumu, KiCad kapalı + `.lck` yok).
- [ ] coverage: denetlenen iddia sayısı `scanned`; atlanan iddia `excluded` +
      gerekçeli. `scanned=0` rapor değildir.

## İlgili

[[../pcb-orchestrator/SKILL|pcb-orchestrator]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../dogrulama-matrisi/references/measurement-error-classes.md]] ·
[[../sematik-erc/SKILL|sematik-erc]] · [[../dfm/SKILL|dfm]] ·
[[../highspeed-routing/SKILL|highspeed-routing]]
