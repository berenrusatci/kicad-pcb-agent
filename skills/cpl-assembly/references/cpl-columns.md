# CPL / BOM kolon sözleşmesi

Kabul kapısı export aracının adı değil, **CPL içeriği + board/commit hash'i +
fabın kabul ettiği paket formatıdır**. Vendor eklentisi zorunlu değildir.

## `cpl.csv` — zorunlu kolonlar

| Kolon | Tip | Kural |
|---|---|---|
| `Designator` | string | Populated BOM refdes kümesine **birebir eşit** olmalı |
| `MidX` | mm, sayısal | **Gövde/courtyard centroid**, footprint origin DEĞİL |
| `MidY` | mm, sayısal | Board bounding box **içinde** olmalı |
| `Layer` | `top` \| `bottom` | Başka değer kabul edilmez |
| `Rotation` | derece, sayısal | `footprint_angle + fab_offset(side, package_class)` |

Türetilmiş/opsiyonel kolonlar (`Comment`, `Package`, `MPN`) fabın şablonuna göre
eklenir; **zorunlu beşlinin semantiği değişmez.**

### Doğrulama invariantları

```
set(cpl.Designator)            == set(bom.refdes where populated)
set(cpl.Designator) ∩ set(DNP) == ∅
all(board_bbox.contains(MidX, MidY))
all(Layer ∈ {top, bottom})
all(isnumeric(Rotation))
```

Bu beş invariant `check_orientation()` içinde koşar. Herhangi biri FAIL ise
gerber paketi üretilmez.

## Rotasyon ofseti

- Ofset **fab-özeldir** (JLC rotasyonu KiCad'inkinden farklıdır) ve
  `rotation_map.json` içinde **versiyonlanır**. Versiyonsuz map = tüm parti
  ters lehim riski → `RED`.
- Ofset `side` bazlıdır: bottom tarafta X mirror + rotasyon işareti ayrıca
  kontrol edilir.
- **Sıfır-yön düzeltmesi tüm paketlere uygulanmaz.** Yalnız polar sınıflar için
  versioned whitelist ve `expected_pin1_or_cathode_side` kullanılır.

Polar sınıflar (whitelist kapsamı): diyot, LED, elektrolitik/tantal kapasitör,
IC pin-1 işaretli paketler, konnektör pin-1, akım-algılama şöntü.

## `bom.csv` — DNP parity

DNP satırları **hem BOM'da hem CPL'de** işaretli olmalıdır:

- BOM'da `DNP` / `Do Not Populate` sütunu işaretli **ve**
- CPL'de o `Designator` **hiç bulunmuyor**.

Sadece birinde işaretliyse montajcı parçayı yine takar. Kapsama giren tipik
DNP grupları:

| Grup | Kaynak |
|---|---|
| İzolasyon 0 Ω / jumper | `dft-testpoints` §3d |
| Shell/ekran seçenek ayak izleri (0 Ω / C / ferrit) | `konnektor` §3e |
| CM choke DNP varsayılanı | `emc-esd` §2 |
| Filtre/choke rezerv alanı | `emc-esd` §2 |
| L3 ayak izleri (DNP filtre, TP) | `dogrulama-matrisi` §Döngüler |

## Fab paketi içeriği

| Dosya | Zorunlu | Not |
|---|---|---|
| Gerber (RS-274X) + drill | ✅ | Üretim tabanı |
| `cpl.csv` | ✅ | Yukarıdaki beş kolon |
| `bom.csv` | ✅ | DNP işaretli |
| F.Paste katmanı | ✅ | Ayrı çıktı; EPAD'lerde windowpane %50–75 |
| Fab notu | ✅ | IPC-4761 Type VII (varsa), sert altın (varsa), empedans hedefi |
| IPC-2581 / ODB++ | ➖ | Fab kabul ediyorsa **yardımcı provenance**; board hash + commit ile saklanır. Tek zorunlu format yapılmaz. |

## Panel

- ≥3 global fiducial (+ BGA'lar için local fiducial)
- ≥5 mm rail
- De-panel (V-score / mouse-bite) hattı hassas parçadan uzak
- Panel şeması **Faz 3 yerleşimine geri beslenir**; Faz 4'te öğrenilmez
