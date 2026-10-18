# Ölçüm hatası sınıfları

> **Bu dosya ölçüm tuzaklarının kanonik sahibidir.** Konsolidasyon öncesi bu
> içerik `dogrulama-matrisi` ve `pcb-highspeed-escape` arasında bölünmüş ve
> `ZONE.GetLayer` tuzağı iki yerde birden yazılıydı.

Yanlış ölçüm, ölçmemekten kötüdür: **sahte ihlal** gerçek işi bloke eder,
**sahte PASS** gerçek kusuru geçirir. Aşağıdaki yedi sınıfın hepsi bu repoda
gerçekten yaşandı ve ölçüldü.

---

## (1) float mm sınır hatası — **iki kez** oldu

`(0.7 - 0.4)/2` float'ta `0.14999999999999997` çıkar; eşik 0.15 mm ise
**tam sınırdaki her öğe ihlal görünür**.

- `dfm_emc_check.annular_ring`: USB-C J1'in 16 padi (halka tam 0.15 mm)
  yanlış ihlal verdi.
- `mask_dam.mask_dam_channel`: baraj tam 0.20 mm'ye çekildikten sonra hâlâ
  FAIL dedi (`0.15 − (−0.025) − (−0.025)`).

**Kural:** eşik karşılaştırmasını **aracın iç biriminde (nm, tam sayı)** yap;
float mm yalnız *raporlama* için. Bir kontrol "tam sınırdaki" bir öğede ne
diyor — testi yazarken bunu ayrıca dene.

---

## (2) Boş karşılaştırma = sahte eşitlik

Şematikteki cache'lenmiş sembolü kütüphane sürümüyle değiştirdikten sonra
"netlist aynı mı?" kontrolü **True** dedi — çünkü regex iki tarafta da **0 net**
çıkarmıştı. `0 == 0` eşitlik değil, **kapsam yokluğudur**. (Gerçekte ERC
1 → 9 uyarıya çıkmıştı.)

**Kural:** her karşılaştırma önce **kapsam** basar (`n_sol`, `n_sağ`); biri 0
ise sonuç `NO_COVERAGE`, asla PASS.

---

## (3) Araç birimi/etiketi yalan söyleyebilir

`kicad-cli sch erc --format json` (10.0.5) koordinat ve uzunlukları **100'e
bölünmüş** yazıyor: gerçek `(82.55, 99.06) mm` → JSON'da `(0.8255, 0.9906)`;
2.54 mm'lik tel "length 0,0254 mm" görünüyor. `coordinate_units: "mm"` diyor
ama değer mm değil. Bu yüzden bir oturumda bu tel "kaynağı bulunamayan"
diye açık madde kalmıştı.

**Kural:** araç çıktısındaki koordinatı kaynak dosyada **birebir aramadan**
kullanma; bulamıyorsan ölçek (×100, ×25.4) dene ve bulguyu tool-bug olarak yaz.

---

## (4) `GetClass() == "PCB_TRACK"` YAYLARI ATLAR

Ark'ta `GetClass()` **`"PCB_ARC"`** döner. Sadece `PCB_TRACK` filtrelersen elle
çizilmiş yay köşeleri ve yay meander'ları uzunluğa girmez.

Ölçülmüş sonuç: usb-hs-breakout'ta bu tek satır
"D+ 30.99 mm / D− 29.48 mm, skew 1.508 mm" ürettiyken gerçek değer
**ikisi de 30.987 mm, skew 0.000 mm** idi — yani **var olmayan bir skew raporu**.

**Doğrusu:** `t.GetClass() in ("PCB_TRACK", "PCB_ARC")` **ve** uzunluk için
`t.GetLength()` (yay uzunluğunu döner, kirişi değil).

---

## (5) `footprint.GetBoundingBox()` ipek metnini içerir

Referans/değer metniyle birlikte hesaplar; kart sınırı denetiminde geçerli
yerleşimler yanlışlıkla "kart dışında" diye reddedilir.

Ölçülen örnek: `Fiducial_1mm_Mask2mm` @(2.5, 2.5) → metinle `-6.79..11.79`,
bakır/maske ile `1.23..3.78`.

**Doğrusu:** `GetBoundingBox(False, False)`.

---

## (6) `ZONE.GetLayerName()` YALAN SÖYLER (pcbnew 10.0.5)

Layer id 2 (B.Cu) için `"F.Cu"` döndürür. Zone'ları katman adına göre
listelersen "her iki GND zone'u da F.Cu'da, **B.Cu düzlemi yok**" gibi sahte bir
felaket raporu çıkar — usb-hs-breakout'ta tam olarak bu oldu ve düzlem aslında
yerindeydi (768.62 mm²).

**Güvenilir kaynak:** `GetLayerSet()` ve ham dosyadaki `(layer "...")`.

- `GetLayer()` yalnız **tek-katmanlı** zone'da kullanılabilir; çok-katmanlı
  zone'da `-1` döner ve sahte "katman yok" FAIL'i üretir.
- Üyeliği `z.GetLayerSet().Contains(layer_id)` ile ölç.
- Listeyi `pcbnew.BOARD.GetStandardLayerName(l) for l in z.GetLayerSet().Seq()`
  ile doğrula.
- Dolu alanı katman bazında oku: `z.GetFilledPolysList(layer).Area()`.
- (Not: `PCB_TRACK.GetLayerName()` doğru çalışıyor; bozuk olan ZONE.)

**Fixture şartı:** hem In1+In2 zone'u PASS etmeli hem hedef katmanı çıkarınca
FAIL vermelidir; positive + negative fault-injection olmadan checker canlı
sayılmaz.

---

## (7) Yakınlık sorgularında yarıçapı geometriye göre seç

"Bypass kondansatörünün GND pad'inde kaç via var" sorusunu 1.0 mm yarıçapla
sorarsan, via'lar birbirinden ≥0.7 mm ayrık konduğu için ikinci via 1.093 mm'de
kalır ve **"hâlâ tek via" yanlışı** çıkar.

**Kural:** yarıçapı 1.5 mm yap ve **mesafeleri de bas**, sadece sayıyı değil.

---

## Genel kurallar

1. Her ölçüm artifact'ında `metric_definition`, units, center-vs-edge semantiği,
   region, sampling step ve geometry model **zorunlu**. Arc'ı chord ölçen araç
   arc-aware sonucu temsil edemez → `INVALID_METRIC`.
2. "0 sorun" ile "kontrol çalışmadı" raporda **aynı görünür**. Bayrağı unutma
   (`--schematic-parity`, `--severity-all`, `--exit-code-violations`).
3. Fault-injection zorunlu: kusuru enjekte et, kontrol **KIRILMALI**. Kırılmıyorsa
   kontrol boştur.
4. Kendi düzeltmen bir kontrolü ilk kez tetikliyorsa (17 parity + 186 silk ihlali
   gibi) bu **iyi haberdir**: kontrol canlı demektir.
5. Aynı ölçümü `step` ve `step/2` ile koş; verdict değişirse veya delta
   toleransın dörtte birini aşarsa `INCONCLUSIVE`.
