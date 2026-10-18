---
name: dft-testpoints
description: Güç rayı + debug (SWD/UART) + kritik sinyallere test noktası; sınıf bazlı kapsama muhasebesi ve bring-up checklist. TP'ler şematikte doğar, erişilebilirlik Faz 4'te kanıtlanır.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 2 (erken: TP'ler şematiğe girer) + Faz 4 (geç: erişilebilirlik + bringup)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: netlist.xml, schema: null, required: true}
  - {artifact: test_access_rules.json, schema: references/testpoint_plan.schema.json, required: false}
  - {artifact: routed board, schema: null, required: false}
outputs:
  - {artifact: testpoint_plan.json, schema: references/testpoint_plan.schema.json}
  - {artifact: dft_coverage_receipt.json, schema: references/testpoint_plan.schema.json}
  - {artifact: bringup_checklist.md, schema: null}
writes_board: true
tools: [testpoint-plan, sch-wire, kicad-cli, boot-observability, shunt-plan]
---

# SKILL: DFT / Test Noktaları

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Artifact adı: [[../_shared/glossary.md]] §7 ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §7

> **Kanonik artifact adı `testpoint_plan.json`'dır.** Konsolidasyon öncesi aynı
> dosya `testpoint_map.json` adıyla da dolaşıyordu ve tüketiciler birbirini
> bulamıyordu. `testpoint_map.json` adı **kullanımdan kaldırılmıştır**.

**İki fazda koşar:**
- **Faz 2 (erken):** TP'ler **şematikte** doğar (adım 1–2). Aynı `.kicad_sch`'e
  yazdığı için [[../sematik-erc/SKILL|sematik-erc]] ile **seri**; wire/label
  kuralları aynen geçerli (TP pin'i wire ile net'e bağlanmalı, havada label
  yeterli değil).
- **Faz 4 (geç, salt-okuma):** yerleşim/routing sonrası TP'lerin mekanik
  erişilebilirliği kanıtlanır (stub yaratmıyor, prob açısı uygun) +
  `bringup_checklist.md` yayınlanır (adım 3).

## 0. Ne zaman çağrılır

- **Faz 2**, `rail_tree.json` + netlist hazır olduğunda ve şematik sahibi
  serbest kaldığında. Placement **tüketmez**; yalnız netlist + rail + debug
  gereksinimiyle TP'leri şematiğe doğurur.
- **Faz 4**, routed board üzerinde salt-okuma erişilebilirlik ölçümü için.
  FAIL placement'a feedback açar; kanonik board sessizce yamalanmaz.

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| rail listesi | `rail_tree.json` | zorunlu kapsam hesaplanamaz |
| net sınıfları (POWER/DEBUG/CLOCK) | netlist | `NO_COVERAGE` |
| pad çapı / pitch / kenar mesafesi / probe class | `test_access_rules.json` (test evi kılavuzu, kaynak+tarih) | **kural yoksa mikro-TP'yi erişilebilir sayma** |
| BSDL / silicon revision / IDCODE | cihaz üreticisi | boundary-scan kapsamı yazılamaz |
| üretim hacmi / ECO sıklığı | ürün planı | fixture kararı `CONFIRM` |

## 2. Adımlar

1. `insert_test_points(nets, rules)` → sınıf POWER/DEBUG/CLOCK net'lerine TP.
   Şematik S-Expr deseni [[../sematik-erc/SKILL|sematik-erc]] §3 ile aynı
   (parantez-blok + `.bak` + idempotent).
   - Zorunlu kapsam: her güç rayı (regülatör çıkışı) + SWD (SWDIO/SWCLK/nRST) +
     UART (TX/RX) + boot-strap + PMIC enable/PG.
2. Erken adımda `testpoint_plan.json` yayınla; fiziksel koordinat/erişilebilirlik
   PASS'i **verme** çünkü placement henüz yoktur. Footprint'ler şematik→PCB sync
   ile Faz 3 placement owner'ına gider.
3. Faz 4'te board'u **değiştirmeden** `verify_testpoint_access()` çalıştır;
   top-side, courtyard/probe açısı, GND yakınlığı ve high-speed stub koşullarını
   ölç. FAIL placement'a feedback açar. `generate_bringup_checklist()` ile rail
   sırasını yayınla.
4. Test stratejisini paket yoğunluğuna göre seç: IEEE 1149.1 boundary-scan
   dijital interconnect kapsamasını artırır; güç/analog ve scan-dışı netler için
   fiziksel probe erişimi yine gerekir. Bed-of-nails/flying-probe kararını
   hacim, erişilebilir alan, NRE ve hedef kapsama ile kaydet.
   - **Boundary-scan kapısı:** BSDL mevcut, chain doğrulanmış, TAP erişilebilir.
     Diferansiyel/AC-coupled ağ kapsamını yalnız cihazlar IEEE 1149.6
     destekliyorsa yaz.
   - **Bed-of-nails kapısı:** üretim hacmi ve tasarım kararlılığı fixture
     maliyetini haklı çıkarıyor. **Flying probe kapısı:** düşük hacim/NPI veya
     sık ECO.
   - Hiçbiri tek başına güç+analog+dijital kapsamı kapatmıyorsa `hybrid` seç.
5. **Coverage'ı tek yüzdeyle toplama.** `defect_universe[]` içinde bare-board,
   assembled interconnect, component presence/orientation/value, powered
   digital structural, power rail, analog/manual ve functional sınıflarını ayrı
   **denominator'larla** say. Her satır `primary_method`, `fallback_method`,
   `evidence_artifact`, `covered` ve `uncovered_reason` taşır.
   - Boundary-scan kaydı BSDL kaynak/revizyonu, silicon revision, IDCODE,
     netlist/layout hash ve chain doğrulama tarihini taşır. 1149.6 kapsamını
     1149.1'e **otomatik katma**.
   - Fiziksel erişim kaydı pad çapı, pitch/spacing, kenar mesafesi, side, probe
     class, en yakın GND ve fixture/flying-probe erişilebilirliğini taşır.
   - Power/analog/manual sonuçlarını boundary-scan yüzdesine **katma**.

## 3. Nüanslar — bilinen tuzaklar

### (a) Yüksek hızlı hatta TP stub'ı yok
Empedans süreksizliği yaratır → mikro-TP / prob-pad kullan. Bu netlerde kapsama
hedefi düşer ve `uncovered_reason` ile yazılır
([[../_shared/rule-conflicts.md]] §7).

### (b) Osiloskop için TP'ye < birkaç mm GND TP
Uzun GND yaylı prob ölçümü, ölçtüğün şeyi değil kurulumunu ölçer.

### (c) Rail sıralama hatası core'a hasar verir
`bringup_checklist.md` sırayı **zorlar**; PMIC PG → sonraki enable zinciri
şematikte kurulur ([[../sematik-erc/SKILL|sematik-erc]] §5).

### (d) İzolasyon jumper'ları BOM'da DNP işaretli
0 Ω/DNP izolasyon jumper'ları hem BOM'da hem CPL'de işaretli olmalı
([[../cpl-assembly/SKILL|cpl-assembly]] §DNP parity).

### (e) "%100 DFT coverage" diye genelleme
Güç + debug düğümlerinde hedef %100 **kalır**; bunu tüm kart için "%100 DFT
coverage" diye genelleme. Sınıf bazlı denominator'lar bu yüzden ayrıdır.

### (f) Test pad geometrisini evrensel sayı olarak gömme
Pad çapı, spacing ve kenar mesafesini seçilen test evinin probe kılavuzundan
kaynak + tarihle `test_access_rules.json` içine al. **Kural yoksa mikro-TP'yi
erişilebilir sayma.**

## 4. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek prob erişimi | C→D | Fixture/flying-probe denemesi |
| Boundary-scan chain çalışması | C→D | Fiziksel chain doğrulama |
| Bring-up sıralamasının doğruluğu | D | Gerçek kartta ölçüm |
| Fonksiyonel kapsama | D | Fonksiyonel test istasyonu |
| Erken fazda koordinat/erişilebilirlik | — | Placement henüz yok → PASS verilmez |

## 5. Doğrulama

```bash
# erken: şematik TP'leri netlist üzerinden doğrula
python3 tools/tests/test_sch_wire.py

# geç: salt-okuma erişilebilirlik + kapsama makbuzu
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

Fault-injection: bir rail'in TP'sini sil → zorunlu kapsam kapısı **FAIL vermeli**.
Bir TP'yi courtyard'ın altına taşı → erişilebilirlik kontrolü **FAIL vermeli**.

## 6. Kabul kriterleri

- [ ] Zorunlu kapsam tam: her güç rayı + SWD (SWDIO/SWCLK/nRST) + UART (TX/RX) +
      boot-strap + PMIC enable/PG.
- [ ] TP pinleri **wire ile** net'e bağlı; havada label yok.
- [ ] Erken fazda fiziksel koordinat/erişilebilirlik PASS'i **verilmedi**.
- [ ] Faz 4 erişim kontrolü board'u **değiştirmeden** koştu; FAIL placement'a
      feedback açtı.
- [ ] Her net için `method: boundary_scan|bed_of_nails|flying_probe|manual` ve
      `coverage_reason` yazılı; yöntemi belirtilmeyen net kapsanmış sayılmadı.
- [ ] `defect_universe[]` sınıfları **ayrı denominator'larla** sayıldı; tek
      yüzde toplaması yok.
- [ ] Power/analog/manual sonuçları boundary-scan yüzdesine katılmadı;
      1149.6 kapsamı 1149.1'e otomatik katılmadı.
- [ ] Boundary-scan kaydı BSDL kaynak/revizyon, silicon revision, IDCODE,
      netlist/layout hash ve chain doğrulama tarihi taşıyor.
- [ ] Fiziksel erişim kaydı pad çapı, pitch, kenar mesafesi, side, probe class,
      en yakın GND ve fixture erişilebilirliği taşıyor.
- [ ] `test_access_rules.json` kaynak + tarih ile mevcut; yoksa mikro-TP
      erişilebilir sayılmadı.
- [ ] Yüksek hızlı hatlarda TP stub'ı yok; bu netlerde `uncovered_reason` yazılı.
- [ ] Osiloskop TP'lerinin birkaç mm içinde GND TP var.
- [ ] İzolasyon 0 Ω/DNP jumper'ları BOM **ve** CPL'de işaretli.
- [ ] `bringup_checklist.md` rail sırasını zorluyor.
- [ ] "%100 DFT coverage" iddiası yok; hedef yalnız güç+debug için %100.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../sematik-erc/SKILL|sematik-erc]] · [[../guc-isil/SKILL|guc-isil]] ·
[[../konnektor/SKILL|konnektor]] · [[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../cpl-assembly/SKILL|cpl-assembly]] · [[../dfm/SKILL|dfm]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`boot-observability`** — *“Kart açılmıyor, hangi ray gelmedi?”* — enable grafiğini TP kapsamıyla çaprazlar; “TP'si olan ray” değil **ayırt edilebilir arıza sınıfı** sayar.
- **`shunt-plan`** — Ray başına **tahribatsız** akım ölçümü için seri şönt + prob test noktası planı.
