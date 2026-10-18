# Sözlük — adı çakışan büyüklükler

> Konsolidasyonda tespit edilen en tehlikeli sınıf: **iki farklı fiziksel
> büyüklüğün aynı adı taşıması.** Aşağıdaki ayrımlar zorunludur; skill metinleri
> ve CLI bayrakları bu adları kullanır.

---

## 1. `f_knee_edge` ≠ `f_nyquist` (çakışma C1)

Konsolidasyon öncesi her ikisi de "`f_knee`" adıyla geçiyordu ve **aynı CLI
bayrağını** (`--f-knee-ghz`) besliyordu.

| Ad | Tanım | Neyi belirler | Kaynak |
|---|---|---|---|
| **`f_knee_edge`** | `0.35 / t_r` (t_r = %10–90 kenar süresi) | Kenarın taşıdığı **spektral enerji üst sınırı**. Işıma, dikiş (stitching) aralığı, filtre bandı, guard via λ/20 hedefi | Bogatin/Johnson kenar-knee yaklaşımı |
| **`f_nyquist`** | `veri_hızı / 2` (NRZ) | Kanalın taşıması gereken **en düşük** bant genişliği. Via-stub rezonans marjı, kayıp bütçesi, S-parametre bandı | NRZ Nyquist |

**Hangisi nerede kullanılır:**

- **EMC / stitching / kenar via çiti / CM filtre bandı → `f_knee_edge`.**
  Işıma kenar hızından doğar, sembol hızından değil. 100 MHz saat + 500 ps kenar
  → `f_knee_edge` = 700 MHz.
- **Via stub / kanal bant genişliği / S-parametre marjı → `f_nyquist`.**
  Rezonansın kanalın gerekli elektriksel bant genişliğinin `>2×` üstünde
  kalması bu büyüklükle ölçülür.

**Neden karıştırılamaz:** 2.5 Gb/s / 150 ps kenarlı bir linkte
`f_knee_edge` = 2.33 GHz, `f_nyquist` = 1.25 GHz. λ/20 dikiş aralığı ikisi
arasında **~1.9×** değişir — yani yanlış olanı seçmek dikiş yoğunluğunu iki
katına çıkarır veya yarıya düşürür.

**CLI sözleşmesi:** `stitch_density.py` bayrağı `--f-knee-edge-ghz` olarak
okunmalıdır. Araç hâlâ `--f-knee-ghz` adını taşıyorsa, çağıran skill besleneni
`f_knee_edge` olarak **receipt'e yazar**; hangi büyüklüğün verildiği
`metric_semantics` içinde açıkça durur. Etiketsiz besleme `INVALID_METRIC`.

---

## 2. `parallelism_coverage` ≠ elektromanyetik kuplaj (çakışma C2)

Repo genelinde "coupling" diye yerleşmiş metrik (`coupling.py`, HANDOFF,
commit'ler) **elektromanyetik kuplajı ölçmez.** Ölçtüğü şey: çiftin nominal
adımda paralel gittiği **örneklem yüzdesi**.

- Kanonik ad: **`parallelism_coverage`**.
- Tolerans (kaç mm'ye kadar "paralel" sayılıyor), `sampling_step_mm` ve ölçülen
  segment **yazılmadan** rapor edilemez. Yazılmamış yüzde `INVALID_METRIC`.
- `parallelism_coverage` **hiçbir zaman** `impedance_pass` üretmez. Field solver
  veya lab kanıtı yoksa sonuç en fazla `GEOMETRY_PRECHECK_PASS`.
- Aynı kartta tolerans 0.70 mm → %64.1, tolerans 1.00 mm → %88.6. Yani toleransı
  yazmadan verilen yüzde **istenen sonucu üretecek şekilde seçilebilir**.

---

## 3. `ipc2221_screen_width_mm` ≠ `final_width_mm`

- `ipc2221_screen_width_mm`: hızlı, muhafazakâr **eleme**. `A=(I/(k·ΔT^0.44))^(1/0.725)`,
  k = 0.048 dış / 0.024 iç. Yalnız ön-elektir; tek başına kabul kriteri değildir.
- `final_width_mm`: IPC-2152 baseline + stackup/düzlem yakınlığı/bakır
  kalınlığı/ortam düzeltmeleriyle. Tasarımı donduran sayı budur.
- `max_ir_drop_mv`: **ayrı** hesaplanır. Güç izinde asıl kısıt genelde ΔT değil
  IR drop'tur.

---

## 4. `θ_ja` ≠ `ψ_jt` ≠ `θ_jb`

- `θ_ja` bir **JEDEC test kartı** özelliğidir (1s0p / 2s2p), cihaz özelliği
  değil. Yalnız kaba eleme (`SCREEN_ONLY`).
- `ψ_jt` bir termal direnç **değildir**, bir ölçüm katsayısıdır:
  `Tj_est = T_top + ψ_jt · P`. Ölçüm noktası ve koşulu (akış, ortam, yük)
  kaydedilmeden taşınmaz.

---

## 5. HBM ≠ IEC 61000-4-2

| | Üreteç | Karakter |
|---|---|---|
| HBM (bileşen, JEDEC) | 1500 Ω / 100 pF | montaj hattı içindir |
| IEC 61000-4-2 (sistem) | 330 Ω / 150 pF | 8 kV kontakt ≈ **30 A tepe**, t_r 0.7–1 ns |

IC'nin "2 kV HBM" etiketi, konnektöre gelen sistem darbesi hakkında **hiçbir şey
söylemez**. İki sayı karşılaştırılamaz.

---

## 6. Maske barajı (`mask_dam`) ≠ bakır clearance

`baraj = bakır_boşluk − expansion(a) − expansion(b)`

DRC "clearance" barajı **görmez**. Ayrı `solder_mask_bridge` kontrolü ve bu
hesap gerekir. Eşik değeri ve karar bandı: [[rule-conflicts.md]] §C5.

---

## 7. Artifact adı: `testpoint_plan.json` (çakışma C4)

Konsolidasyon öncesi aynı artifact iki adla dolaşıyordu
(`testpoint_map.json` / `testpoint_plan.json`) ve tüketiciler birbirini
bulamıyordu. **Kanonik ad `testpoint_plan.json`'dır.** `testpoint_map.json`
adı kullanımdan kaldırılmıştır; hiçbir üretici veya tüketici bu adı kullanmaz.
Geç faz kapsama makbuzu ayrı bir artifact'tir: `dft_coverage_receipt.json`.

---

## 8. `fanout_coverage` ≠ `parallelism_coverage`

İkisi de "paralel" sözcüğünü taşır ve **hiç ilgileri yoktur**. Ad benzerliği
§2'nin (C2) aynı sınıfıdır: iki farklı büyüklüğün yakın adla dolaşması.

| Ad | Neyin yüzdesi/sayısı | Sahibi |
|---|---|---|
| **`parallelism_coverage`** | İki **izin** nominal adımda paralel gittiği örneklem yüzdesi — bir geometri ölçüsü | [[../highspeed-routing/SKILL\|highspeed-routing]] §4 |
| **`fanout_coverage`** | Kaç **iş biriminin** fan-out kapısından geçirildiği (`eligible/scanned/excluded/violations`) — bir yürütme sayacı | [[../paralel-yurutme/SKILL\|paralel-yurutme]] |

- `fanout_coverage` hiçbir zaman bir elektriksel/geometrik iddia üretmez;
  `violations` orada **yol kesişimi** demektir, kuplaj veya ihlal geometrisi
  değil.
- `parallelism_coverage` hiçbir zaman bir yürütme kararı üretmez; iki dalın
  paralel koşup koşamayacağı hakkında hiçbir şey söylemez.
- Bir receipt ikisini de taşıyorsa alan adları **açıkça** yazılır; etiketsiz
  "paralellik %" değeri `INVALID_METRIC`'tir.

Makine tarafında ad sabitlenmiştir: `fanout_plan.json` şemasındaki
`metric_name` alanı `const: "fanout_coverage"`'dır ve regresyon bunu ölçer
(`tools/tests/test_paralel_yurutme.py::test_the_metric_name_avoids_the_c2_collision`).
