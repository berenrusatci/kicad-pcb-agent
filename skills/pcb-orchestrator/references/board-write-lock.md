# Board / şematik yazımı kilidi ve transaction

> **ZORUNLU.** Aynı `.kicad_pcb` / `.kicad_sch`'e **tek ajan** yazar.
> Paralel dallar yalnızca **ayrı artifact/dosya** yazabilir. Dosyaya yazan her
> şey seridir.

---

## 1. Yazım öncesi kilit kanıtı

Yazımdan önce KiCad'in kapalı olduğu **kanıtlanır**:

```bash
pgrep -a 'kicad|pcbnew'
ls -la <proje>/*.lck
lsof <board.kicad_pcb> <schematic.kicad_sch>
```

**`pgrep` sonucunu tek başına karar sayma** — MCP server adı yanlış pozitif
verebilir. Yönetici şu **dört kanıtı birlikte** değerlendirir:

1. gerçek GUI process'i,
2. `*.lck` dosyası,
3. `lsof <board/schematic>`,
4. `/proc/*/fd` açık dosya taraması.

**Worker stale lock'u silmez**; yalnız kanıt paketini yöneticinin kararına sunar.

Her yazımdan önce `.bak`; sonuç git'e commit (scratch tek kopya olmasın).

---

## 2. Board yazımı bir transaction'dır

Her board-yazan adım şu sırayı izler; **ara adım atlanmaz**:

```
temiz input hash
  → ayrı scratch dizini
    → mutasyon
      → L0 receipt
        → yalnız PASS ise kanonik dosyaya atomik promote
          → git commit/tag
```

- **FAIL'de kanonik dosyaya dokunma.** Scratch + makbuzu kanıt olarak sakla.
- Promotion receipt ayrıca scratch path, artifact hash'leri, tam komutlar,
  exit code'lar, `promotion: GREEN|RED`, RED gerekçe kodları ve retention
  policy/expiry taşısın. **DRC snapshot temiz olsa bile geometry kapısı FAIL ise
  RED.**
- GREEN promotion **atomik işlem kanıtı** (`atomic: true` veya exact `--atomic`
  komutu) taşır.
- **Retention:** RED/ERROR/NO_COVERAGE'da receipt + child + log + scratch;
  GREEN'de receipt + hash + özet metrik + yeniden üretim komutu audit penceresi
  boyunca saklanır.
- RED promotion her canonical target için `before_sha256`, `after_sha256` ve
  current `sha256` **eşitliğini kanıtlar**. Eşit değilse canonical değişmiştir;
  promotion-contract ihlali olarak raporlanır.

---

## 3. Türetilmiş (derived) artifact'ler

Zone fill, teardrop, uzunluk raporu ve fab çıktıları **türetilmiştir**:
checkpoint'e bağlanır ve **promote sonrası yeniden üretilir**.

Bir board yazımı bunları sessizce bayatlatır:

| Artifact | Sahibi |
|---|---|
| zone fill | dfm |
| teardrop (`IsTeardropArea()` sayısı) | dfm / highspeed-routing |
| `length_report.json`, `coupling_report.json`, `reference_report.json` | highspeed-routing |
| `escape_report.json` | highspeed-routing |
| `crosstalk_scan.json` | crosstalk-thinning |
| `emc_geometry_receipt.json` | emc-esd |
| `gerbers.zip`, `cpl.csv` | cpl-assembly |
| `autoroute_receipt.json` | otonom-routing |

---

## 4. Geri alınamayan yan etkiler

**Dış servis çağrıları (fab RFQ, API sorgusu) geri alınamaz yan etkidir:**
ayrı loglanır. Dosya rollback'i bunları geri almaz — aldı sanma.

Bu kapsama girenler:
- Fab RFQ / sipariş gönderimi
- Nexar/Octopart sorguları (kota tüketir)
- Git push / tag
- Herhangi bir e-posta/bildirim

---

## 5. Board yazan skill'ler

`writes_board: true` taşıyan skill'ler — bunlar **asla paralel koşmaz**:

| Skill | Yazdığı dosya | Serileştiği skill |
|---|---|---|
| [[../../sematik-erc/SKILL\|sematik-erc]] | `*.kicad_sch` | dft-testpoints (Faz 2) |
| [[../../dft-testpoints/SKILL\|dft-testpoints]] | `*.kicad_sch` (Faz 2) | sematik-erc |
| [[../../dfm/SKILL\|dfm]] | `*.kicad_pcb` (yerleşim) | highspeed-routing |
| [[../../highspeed-routing/SKILL\|highspeed-routing]] | `*.kicad_pcb` (routing) | dfm |
| [[../../otonom-routing/SKILL\|otonom-routing]] | `*.kicad_pcb` (kalan netler; yalnız **scratch** kopya, promote orkestratörde) | dfm, highspeed-routing |

Salt-okunur skill'ler (`writes_board: false`) paralel koşabilir ve **ayrı
artifact** yazar: guc-isil, mekanik-dxf, bom-lifecycle, konnektor,
empedans-stackup, crosstalk-thinning, emc-esd, cpl-assembly,
dogrulama-matrisi, decompose-pcb-decisions, claim-audit, pcb-orchestrator.

> `crosstalk-thinning` ve dört board checker'ı (`dfm_emc_check`, `mask_dam`,
> `stitch_density`, `via_stub`) **hiçbir board dosyasına yazmaz** ve bu yüzden
> MCP üzerinden güvenle yayımlanır. `rewire`, `wireify` ve
> `context_pack --postflight` **yayımlanmaz**.
