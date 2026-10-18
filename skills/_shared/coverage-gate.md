# Ortak coverage kapısı

> Bu dosya, skill dosyalarında **kelimesi kelimesine tekrarlanan** kapı
> paragrafının tek kanonik kaynağıdır (konsolidasyon öncesi D1). Skill'ler bunu
> tekrar etmez; yalnız frontmatter'da `assurance_loop` ve `evidence_origin`
> alanlarını yayınlar ve buraya bağ verir.

## Kural

Her sayılabilir metrikte `eligible / scanned / excluded / violations` yayınla.

```
scanned + excluded == eligible
violations        <= scanned
scanned           <= eligible
```

- `scanned = 0` ise sonuç **`NO_COVERAGE`**'dır — asla `GREEN` değildir.
- Kapsam aritmetiği tutmuyorsa `INVALID_COVERAGE/RED`.
- Kaynaksız yerel fallback yalnız `LOCAL_POLICY` / `SCREEN_ONLY` üretir.
- Eksik kanıt `CONFIRM`'dür; `PASS` değildir.

Makine-okur iskelet: [`tools/schemas/metric_coverage.schema.json`](../../tools/schemas/metric_coverage.schema.json)

## `evidence_origin` sözlüğü

| Değer | Anlamı |
|---|---|
| `TOOL_REPORT` | Deterministik bir aracın çıktısı (exit code + parsed metric ile) |
| `LOCAL_POLICY` | Bu repoda seçilmiş, otorite olmayan varsayılan. PASS üretemez. |
| `AUTHORITATIVE_SOURCE` | Üretici/standart belgesi; `document_id` + `revision` + `locator` + `accessed` + `snapshot_sha256` zorunlu |
| `DESIGN_ARTIFACT` | Akışın kendi ürettiği, şemaya bağlı artifact |
| `PHYSICAL_MEASUREMENT` | L3 lab/kupon ölçümü (kurulum + baseline + fark kaydıyla) |

## `assurance_loop` sözlüğü

| Döngü | Ne zaman koşar | Kanıt |
|---|---|---|
| `L0` | Her board/şematik yazımından sonra, otomatik | script çıkış kodu |
| `L1` | Faz sınırı (kalite kapısı) | gate receipt |
| `L2` | Geri besleme kenarı (DAG çevrimi) | değişen upstream artifact |
| `L3` | Fiziksel döngü (kart geldikten sonra) | ölçüm kaydı |

Seviye taksonomisi (A/B/C/D) ve döngü tanımlarının tamamı:
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]].

### `assurance_loop` **sahiplik taşımaz** — participates-in ≠ produces-evidence-for

Ölçüm (2026-08-10): 10 skill frontmatter'ında `L3` listeliyordu
(`crosstalk-thinning`, `dft-testpoints`, `dogrulama-matrisi`, `emc-esd`,
`empedans-stackup`, `guc-isil`, `highspeed-routing`, `konnektor`,
`mekanik-dxf`, `pcb-orchestrator`) ve **18/18'inde**
`evidence_origin=TOOL_REPORT` yazıyordu — yani tek bir
`PHYSICAL_MEASUREMENT` üreticisi yoktu. Etiket "bu skill L3'ü yürütür"
diye okunuyor, gerçekte "bu konu sonunda L3 ister" diyordu. Kapanmış
görünen bir boşluk, kapanmamış bir boşluğun en sessiz hâlidir.

Yeni metadata alanı **eklenmedi**; ayrım zaten var olan iki alanın
birleşiminde okunur:

| Okuma | Nasıl anlaşılır | Örnek |
|---|---|---|
| **participates-in `Lx`** | `assurance_loop` içinde `Lx` var, fakat o döngünün kanıt kökeni `evidence_origin`'de **yok** | `guc-isil`: `assurance_loop: [L1, L3]`, `evidence_origin: TOOL_REPORT` → ısıl doğrulama **ister**, ölçmez |
| **produces-evidence-for `Lx`** | `assurance_loop` içinde `Lx` **ve** o döngünün kökeni `evidence_origin`'de | `l3-bringup-karakterizasyon`: `assurance_loop: [L2, L3]`, `evidence_origin: [PHYSICAL_MEASUREMENT, …]` |

Döngü ↔ zorunlu köken eşlemesi:

| Döngü | Üretici olmak için gereken `evidence_origin` |
|---|---|
| `L0` | `TOOL_REPORT` (script çıkış kodu) |
| `L1` | `TOOL_REPORT` veya `DESIGN_ARTIFACT` |
| `L2` | üretici değil; **tetikleyici** — kanıt yeniden koşan upstream'in |
| `L3` | **`PHYSICAL_MEASUREMENT`** |

Kural: **`PHYSICAL_MEASUREMENT` yayımlamayan bir skill, `L3` listelese
bile o kuralı kapatamaz** — ilgili satır kendi "kanıtlayamaz" tablosunda
D/L3 olarak kalır ve kapanışı L3 üreticisine devredilir. Ters yön de
yasaktır: `PHYSICAL_MEASUREMENT` yayımlayan skill limit/threshold
tanımlayamaz; limitin sahibi domain skill'i olarak kalır
([[rule-conflicts.md]] §9).

## Verdict sözlüğü

`GREEN` · `RED` · `CONFIRM` · `SCREEN_ONLY` · `LOCAL_POLICY` · `SIM_ONLY` ·
`NO_COVERAGE` · `INVALID_METRIC` · `INVALID_COVERAGE` · `INVALID_SPARAM` ·
`INCONCLUSIVE` · `STALE` · `GEOMETRY_PRECHECK_PASS` · `TBD` · `API_YOK`

Bir skill bu sözlüğün dışında verdict üretmez.

### Verdict isim alanı — `gate.verdict` ≠ domain/workflow state

Sözlük kapalı olduğu için, **kapı kararı olmayan** üçlüler onun içine
sızdığında sessiz bir sözleşme ihlali oluşur: okuyucu (ve assertion
seçicisi) ikisini ayırt edemez. Ölçülmüş çakışmalar:

| Değer kümesi | Nerede | Ne olduğu | Nereye yazılır |
|---|---|---|---|
| `DOĞRU` / `YANLIŞ` / `DOĞRULANAMAZ` | [[../claim-audit/SKILL\|claim-audit]] | dış iddianın **domain state**'i | `claim_state` |
| `SAFE_TO_THIN` / `CROSSTALK_RISK` / `THINNING_BLOCKED` | [[../crosstalk-thinning/SKILL\|crosstalk-thinning]] | tarama sonucunun **domain state**'i | `domain_state` |
| `NEEDS_HUMAN` | [[../pcb-orchestrator/references/phase-dag.md\|phase-dag]] §L2 | döngü sınırı aşımının **workflow state**'i | `workflow_state` |

Kural (üç madde, hepsi zorunlu):

1. Kapalı sözlük yalnız **`gate.verdict`** (ve `gate_receipt.json`'daki
   `verdict`/`status`) alanı içindir. Bir rapor bu alana sözlük dışı bir
   sözcük yazamaz.
2. Domain/workflow sonucu ayrı bir alana yazılır (`claim_state`,
   `domain_state`, `workflow_state`) ve o alan **kapalı sözlüğe tabi
   değildir**; kendi sözlüğünü kendi skill'i tanımlar.
3. Aynı raporda ikisi birden varsa **`gate.verdict` bağlayıcıdır**.
   `domain_state=SAFE_TO_THIN` bir kapıyı açmaz; kapıyı açan
   `gate.verdict=GREEN`'dir ve o da coverage aritmetiğine tabidir.

> Geriye dönük not: `common/report_io.py::status_exit_code` `SAFE_TO_THIN`'i
> hâlâ exit 0'a eşliyor. Bu **çıkış kodu** eşlemesidir, verdict sözlüğüne
> giriş değildir; yeni bir araç domain state'ini oraya eklemek yerine
> `gate.verdict` yayımlar.
