---
name: sematik-erc
description: Şematikte GERÇEK wire çizimi (pin geometrisi + junction + label) + S-Expr enjeksiyonu + ERC auto-loop ve netlist doğrulaması. Bağlantının kanıtı netlist'tir, çizginin görünmesi değil.
version: 1.1.0
last-reviewed: 2026-08-10
phase: "Faz 2 — Elektriksel Altyapı (Adım 1'den sonra; stackup ile paralel, DFT ile seri)"
assurance_loop: [L0, L1]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: parts.json, schema: null, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: interface_contract.json, schema: references/interface_contract.schema.json, required: true}
  - {artifact: esd_chain.json, schema: ../emc-esd/references/esd_chain.schema.json, required: true}
outputs:
  - {artifact: "*.kicad_sch", schema: null}
  - {artifact: netlist.xml, schema: null}
  - {artifact: erc.json, schema: null}
  - {artifact: net_verify.json, schema: null}
  - {artifact: sematik_riskler.md, schema: null}
  - {artifact: strap_binding.json, schema: references/strap_binding.schema.json}
writes_board: true
tools: [sch-wire, sch-wirefirst, wire-audit, sch-route, rewire, wireify, kicad-cli,
        i2c-pullup, block-catalog, dfm-emc-check, assert-matrix, hand-solder-gate]
---

# SKILL: Şematik & ERC

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §2

Üç mod: **(a) üretim** — sembol yerleştir + **wire çiz**; **(b) manipülasyon** —
mevcut `.kicad_sch`'e property/TP enjekte et; **(c) ERC auto-loop + net doğrulama**.

> **Bu skill'in yeniden yazılma sebebi:** önceki script'ler pin'lere sadece
> `(label ...)` koyup "uçan net" bırakıyordu — `usb-hs-breakout` şematiğinde
> **0 adet `(wire`**, 27 label vardı. Şematik boş görünüyor, insan okuyamıyor.
> (a) modu artık zorunludur. Yasak olan **tamamen etiketle bağlanmış sayfa**;
> tel ile etiketin **karışık** kullanımı serbesttir, aşağıdaki karar kuralına bak.

## 0. Ne zaman çağrılır

- Faz 2'de, `rail_tree.json` yayınlandıktan **ve** koruma topolojisi
  (`esd_chain.json`) donduktan sonra. Şematik 9a tamamlanmadan açılmaz.
- **Board yazan bir skill'dir** → tek sahipli, seri. Aynı `.kicad_sch`'e yazan
  [[../dft-testpoints/SKILL|dft-testpoints]] Faz 2 adımı ile **serileştirilir**.
- Yazımdan önce: `pgrep -a 'kicad|pcbnew'` + `*.lck` yok kontrolü + `.bak`.
  Kilit kanıt paketi kuralı: [[../pcb-orchestrator/SKILL|orchestrator]].

## 1. Girdi/çıktı sözleşmesi

| Alan | Kaynak | Yoksa |
|---|---|---|
| net adları (kanonik) | `parts.json` / `rail_tree.json` | isim karışması sahte kopukluk/short üretir |
| `role`, `direction`, akım/inrush/OVP hedefleri | `interface_contract.json` | Rol açık değilse koruma MPN/limit **seçme**, `CONFIRM` |
| koruma sırası | `esd_chain.json` | sıra yanlışsa layout kurtaramaz |
| DC-bias sonrası kapasite | `rail_tree.json → c_eff_uf` | `TBD` → [[../guc-isil/SKILL|guc-isil]] §3b |
| strap/boot tablosu | datasheet | `TBD` + ölçülebilir (TP) + mümkünse DNP ikili |

## 2. (a) Wire çizimi — `tools/scripts/sch/sch_wire.py`

```python
from sch_wire import SchBuilder, load_lib_symbols, verify_nets, run_erc
lib = load_lib_symbols("/usr/share/kicad/symbols/Device.kicad_sym")
sch = SchBuilder(project="brd"); sch.embed(lib["R"], "Device:R")
r1 = sch.place("R1", "Device:R", 100, 100, value="5k1", rot=90)
sch.wire([r1.pin("1").xy, (100, 88.9)])      # pin ankrajından ray'a
sch.wire_pins(r1.pin("2"), r2.pin("1"))      # pin -> pin, ortogonal
sch.label(r1.pin("2"), "USB_CC1")            # stub + etiket (ucuna oturur)
sch.power(r1.pin("2"), "power:GND")          # GND sembolü + wire
sch.auto_junctions()                         # T noktalarına junction
sch.write("brd.kicad_sch")                   # KiCad-kapalı kontrolü + .bak
```

### Pin ankraj geometrisi (sessiz hata kaynağı #1)
1. `.kicad_sym` içinde pin `(at x y angle)` = **elektriksel bağlantı noktası**;
   `angle` pin'in **gövdeye bakan** yönüdür (`Device:R` pin1 `(at 0 3.81 270)`,
   gövde kenarı y=2.54). Wire'ın çıkacağı **dış yön = angle + 180**.
2. Lib frame'de **Y YUKARI**, sheet'te **Y AŞAĞI** → dönüşümün sonunda Y'yi çevir.
3. **Sıra kritik: önce ROTASYON, sonra MIRROR.** Ters sırada 90°/270° + mirror
   kombinasyonlarında **pin 1 ↔ pin 2 yer değiştirir**; şematik gözle doğru görünür,
   netlist sessizce yanlış olur. 12 kombinasyonun tamamı `test_sch_wire.py` içinde
   netlist üzerinden doğrulanır — **bu testi silme, yenisini ona ekle.**

### Bağlantı kuralları (KiCad connectivity)
- Wire ucu ile pin ankrajı **birebir aynı koordinatta** olmalı. `195.57999999999998`
  gibi float artığı = sessiz kopukluk → **1.27 mm grid'e snap** + 2 ondalık yazım
  (`snap()` / `num()`). Sembol orijinlerini de snap'le.
- **Diyagonal wire çizme**; L (2 segment) veya Z (3 segment) ortogonal yol kullan.
- Bir wire **ucu** başka bir wire'ın **ortasına** denk gelirse KiCad T-bağlantısı
  sayar. Ama **sadece kesişen (çapraz geçen) iki wire BAĞLI DEĞİLDİR** — 3+ ucun
  buluştuğu her düğüme `(junction ...)` koy → `auto_junctions()`.
- Etiket **wire'ın ucuna** oturmalı; havada duran label hiçbir şeye bağlanmaz.
- Kullanılmayan pin → `(no_connect ...)`: ERC'yi susturmak için değil, niyeti
  belgelemek için.

### Ne zaman wire, ne zaman label — **karışık (hibrit) mod**

Amaç okunabilirlik ve **kolay eşleştirme**. Ne "her şey tel" ne "her şey etiket";
ikisi bir arada, ama **hangisinin nerede kullanılacağı keyfi değil**:

**Varsayılan hâlâ TEL.** Etiket, tel *okunmayı zorlaştırdığında* devreye girer.
Ölçüt somut — bir bağlantıyı etikete çevirmek için **en az biri** doğru olmalı:

| Etiket meşrudur | Neden |
|---|---|
| Yol uzunluğu > ~60 mm veya 3+ segment/köşe gerekiyor | uzun tel sayfayı okunmaz yapar |
| Yol, aradaki 2+ sembol gövdesinin etrafından dolaşıyor | dolaşan tel = takip edilemez |
| Aynı net 4+ pine dağılıyor (bus/rail dağıtımı: MIPI, DDR, I2C, reset) | yıldız telleme spagetti üretir |
| Bağlantı sayfa/blok sınırını geçiyor | zaten hiyerarşik/global label işi |
| Sembolün karşı kenarları arasında geri dönüş gerekiyor | tel gövdenin etrafını dolaşır |

**Tel zorunlu (etikete çevirmek YASAK):**

| Durum | Yöntem |
|---|---|
| Kısa, aynı blok içi, komşu sembol arası her bağlantı | **pin→pin tel** (`sch_route`) |
| Bir sinyalin **içinden geçtiği** seri eleman (ESD dizisi, seri R, ferrit, akım-ölçüm shunt'ı) | **tel** — sinyalin parçadan girip çıktığı görünmeli |
| Diferansiyel çift'in her iki bacağı (USB D±, MIPI D0±/CLK±) | **tel**, ikisi yan yana ve simetrik |
| Geri besleme / bölücü ağı (regülatör FB, referans) | **tel** |
| Kristal/osilatör ile yük kapasiteleri | **tel** |
| Güç / GND | **power sembolü** (`power:GND`, `power:+3V3`) — çıplak label DEĞİL |

**Etiket kullanırken uyulacaklar (eşleştirmenin çalışması için):**
- Etiket **daima pin'den çıkan ~2.54 mm'lik kısa bir stub telin ucuna** oturur;
  pin'in üzerine doğrudan etiket koymak = uçan net, kabul edilmez.
- Etiket adı **kanonik ve tek**: `USB_DP` her yerde `USB_DP`; `USB_D+`/`DP`
  karışımı sahte kopukluk üretir. Tüm adlar `parts.json`/`rail_tree.json`'daki
  net adı ile birebir aynı olmalı. Kural taşıyan bir netin adının **hiç
  verilmemesi** de aynı sınıftandır ve daha sessizdir → §10.
- Aynı sayfada kalan net → `label` (local). Sayfa dışına çıkan → `global_label`
  ya da hiyerarşik sheet + `hierarchical_label`. Local label'ı sayfa-arası
  bağlantı sanıp kullanma (netlist'te `/VBUS` gibi sheet önekiyle gelir).
- Her etiketli net **en az 2 ayrı yerde** görünmeli. Tek örnekli etiket =
  hiçbir şeye bağlanmayan ölü net; `verify_nets` bunu hata saysın.
- Bir sayfada etiketle bağlanan net sayısı, o sayfadaki toplam net sayısının
  **~%40'ını geçmemeli** ve **`(wire` sayısı asla 0 olmamalı** — geçerse
  yerleşim kötüdür, sembolleri yeniden konumlandır, etiketle kapatma.

### Tel-önce üretim — `wirefirst.py`

Yeni sayfa ÜRETİRKEN (mod a) varsayılan katman budur. `sch_wire` gerçek tel
çizebiliyordu; eksik olan **varsayılan davranış**tı — en kısa yol iki pine aynı
etiketi koymak olduğu için komşu iki bileşen bile adla bağlanıyordu.

```python
wf = WireFirst(builder)                       # tek sayfada single_sheet=True
v3 = wf.trunk("VIN_3V3", y=381.0, span=(139.7, 809.0))   # ray = ÇİZGİ, ad değil
wf.tap(v3, u302.pin("1"))                                # omurga -> pin
wf.series(a, [fb], b, net="VIN_3V3")                     # pasif YOLUN ÜZERİNDE
wf.decouple(u402.pin("6"), [c5003], sheet.gnd)           # pinin DİBİNDE
wf.label(pt, "CSI_D0_P", "naming-on-wire")               # gerekçe ZORUNLU
```

`label()` gerekçesiz çağrılamaz; gerekçe `LABEL_REASONS`'tan seçilir ve
"component-to-component bağlantı" diye bir gerekçe **yoktur**. Tek sayfalı
tasarımda `sheet-to-sheet` de reddedilir.

`close_pin_taps()` yazımdan önce çağrılmalıdır (`Sheet.write` bunu yapar):
bir dalın **ortasına** denk gelen pin, junction olmadan bağlanmaz ve bu sessizdir
— şema kusursuz görünür, netlist `unconnected-(C5001-Pad1)` der.

### Çizildi mi, yoksa adlandırıldı mı — `wire_audit.py`

Netlist "bağlantı var mı"yı kanıtlar, "**nasıl kuruldu**"yu değil. İki güdük +
aynı etiket ile kesintisiz tel, netlistte **aynı** görünür. Farkı gören tek
tanık geometridir:

```bash
python3 tools/scripts/sch/wire_audit.py board.kicad_sch \
        --netlist build/netlist.net --rules rules.json --json build/audit.json
```

Kontroller: A açık pin · **B etiketle dikilmiş yerel bağlantı** · C eksik pasif
(decoupling'in pine **mesafesi** dahil) · D uç-pin çakışması + yakın-ıska ·
E ERC · F junction tutarlılığı · G geometri↔netlist çelişkisi.

`name_stitched_ok` **varsayılan olarak boştur**: "GND zaten adla birleşir"
istisnasını çağıran YAZILI olarak vermelidir, yoksa aranan kusur (ray adına
asılmış kondansatör) affedilmiş olur.

> **KiCad bağlanma kuralı — ölçüldü (KiCad 10.0.5), varsayılmadı.** Aşağıdaki
> §"Router kuralları" ile tutarlıdır, ama bu dosyanın başka bir yerinde ve
> `cam-w-og05b10/scripts/sch_graph.py`'de yazan "telin ucu başka bir tele
> (ortasına dahil) değerse bağlanır" **yanlıştır**:
>
> | durum | bağlanır mı |
> |---|---|
> | iki telin UCU aynı noktada | evet |
> | tel UCU → başka telin ORTASI (T) | **hayır** * |
> | tel ORTASI → tel ORTASI (X) | hayır * |
> | pin → telin UCU | evet |
> | pin → telin ORTASI | **hayır** * |
> | (*) o noktada `(junction)` varsa | evet |
>
> Yani nokta yalnızca **ucu olduğu** tellere bağlanır; `junction` üzerinde
> durduğu **her** teli birleştirir. Junction'ı unutulmuş bir T kozmetik bir
> eksiklik değil, **sessiz kopukluktur**.

### Otomatik pin→pin yönlendirme — `sch_route.py` + `rewire.py`
```bash
python3 tools/scripts/sch/rewire.py board.kicad_sch                     # rapor (dry-run)
python3 tools/scripts/sch/rewire.py board.kicad_sch --write             # mevcut netlist'i KORUYARAK telle
python3 tools/scripts/sch/rewire.py board.kicad_sch --nets hedef.json --write   # hedef netlist'i KUR
```
`hedef.json`: `{"nets": {"NET": ["U1-1","J2-2"]}, "power_nets": {"GND":"power:GND"},
"flag_pins": {"GND":"J1-A1"}, "no_connect": ["J1-A8"],
"method": {"MIPI_D0_P": "label", "USB_DP": "wire"}}`
`method` isteğe bağlı; verilmeyen net'ler için router yukarıdaki hibrit karar
kuralını (uzunluk / köşe sayısı / fanout eşikleri) uygular.

Router kuralları (KiCad connectivity'den türetilmiştir):
- Sembol gövde kutuları engeldir; teller gövdenin **üstünden geçmez** (`body_bbox`).
- **Dik kesişme serbesttir** (bağlantı yaratmaz), sadece maliyetlidir.
- **Yasak:** yabancı telle üst üste/paralel akmak; yabancı telin **ucuna** değmek;
  yolun yabancı telin **üzerinde bitmesi** — üçü de sessiz short üretir.
- **Tüm net'lerin pin çıkışları routing'den ÖNCE rezerve edilir.** Yoksa önce
  route edilen net, sonraki net'in çıkış noktasından geçer ve o nokta uç olunca
  iki net T-bağlantısıyla birleşir (gerçekte yaşandı: USB_D+ / USB_D−).
- Aynı ankrajı paylaşan **stacked pinler** (ör. USB-C'de 4× GND) tekilleştirilir;
  zaten bağlıdırlar, aralarına tel çekilmez.
- Aynı net içinde 3+ dalın buluştuğu her noktaya `(junction)`.
- Yazımdan sonra netlist yeniden alınır ve hedefle karşılaştırılır; **fark varsa
  dosya `.bak`'tan otomatik geri alınır.**

Hiyerarşide alt sayfadaki `hierarchical_label` ile üst sayfadaki
`(sheet ... (pin "AD" ...))` **ismi birebir** eşleşmeli; eşleşmezse net üst
seviyeye çıkmaz (sessiz kopukluk).

## 3. (b) S-Expr enjeksiyon deseni (footprint / property / TP)

1. **Önce `.bak`** (zorunlu).
2. Blok sonunu **parantez-derinliği sayarak** çöz; string içindeki parantezi atla.
   Naif regex iç içe yapıyı keser → bozuk dosya.
3. Property zaten varsa **ATLA** (idempotent). Değer→footprint deterministik map
   (100nF/1uF→0402, 10uF→0603).
4. Enjeksiyonları **pozisyona göre TERSTEN** uygula (offset kaymasın).
5. Canlı oturum gerekiyorsa ham metin değil **kipy/IPC API** (SWIG KiCad 11'de gidiyor).

### Enjeksiyonu bozan üç somut tuzak (usb-hs-breakout'ta yaşandı)

**(i) Girinti varsayımını `Read` çıktısından ÇIKARMA.** `Read` her satıra
`satır-no + TAB` önekler; 2 tab'lık girinti ekranda **3 tab** görünür.
`\n\t\t\t\(property ` regex'i hiçbir şey bulmaz, `last=None` kalır,
`blk.index("(", None)` sıfırdan başlar ve enjeksiyon **sembolün TAMAMEN
DIŞINA**, iki sembolün arasına düşer. Sonuç: geçerli parantez dengesi ama
geçersiz şema. **Girintiyi dosyadan `repr()` ile doğrula**, ve eşleşme
bulunamazsa sessizce devam etme — `raise`.
Gerçek yapı: sheet'teki `(symbol` **1 tab**, property'leri **2 tab**.

**(ii) Gömülü `lib_symbols` alt-birim adları ÖNEKSİZDİR.**
Üst seviye `(symbol "Connector:TestPoint"` ama alt birimler
`(symbol "TestPoint_0_1"` / `"TestPoint_1_1"` — kütüphane öneki **YOK**.
KiCad üst addan `Lib:` önekini atıp alt-birim adıyla eşleştirir; alt birimleri
de `"Connector:TestPoint_0_1"` diye yeniden adlandırırsan eşleşme kırılır.
Aynı tuzak, mevcut bir lib_symbol'ü kopyalayıp yeniden adlandırırken de var
(`Device:R` → `Device:RX` yaparsan çocuklar `R_0_1` kalır ve dosya reddedilir).

**(iii) `kicad-cli` yükleme hatası MESAJSIZDIR** — tek çıktı
`Failed to load schematic`, rc=3. Satır/sebep vermez. Yöntem: **bisect** —
değişiklikleri tek tek uygulayıp ayrı dosyalara yaz, her birini
`kicad-cli sch export netlist` ile yükle, hangisinin patladığını bul.
Kontrol koşusu olarak dosyayı hiç değiştirmeden yaz–oku yap (encoding/locale
kaynaklı bozulmayı eler).

### Test noktası eklerken
Sembolü **mevcut tüm içeriğin dışındaki** boş bölgeye koy, pin ankrajından
kısa stub çek, etiketi stub'ın ucuna oturt. Mevcut bir telin üzerine snap
etmek iki neti sessizce birleştirir. PCB'deki footprint'in parity'yi geçmesi
için `footprint.SetPath(KIID_PATH(f"{sheet_uuid}/{symbol_uuid}"))` gerekir →
şematik UUID'lerini **uuid5 ile deterministik** üret ki iki taraf aynı değeri
bağımsız hesaplayabilsin.

## 4. (c) ERC auto-loop + net doğrulama

```bash
kicad-cli sch erc --format json --severity-error --severity-warning \
  --exit-code-violations -o erc.json X.kicad_sch
kicad-cli sch export netlist --format kicadsexpr -o nl.net X.kicad_sch
# Sematik<->PCB parity: BAYRAK ZORUNLU
kicad-cli pcb drc --schematic-parity --format json --severity-all \
  --exit-code-violations -o drc.json X.kicad_pcb
```

Mandatory ERC/DRC komutunda exact argv receipt ve `--exit-code-violations`
zorunludur. [`tools/fixtures/contract/`](../../tools/fixtures/contract/)
içindeki kasıtlı ERC ihlali nonzero exit ve beklenen violation kaydı üretmeden
sözleşmeyi GREEN sayma. `evidence_origin=TOOL_REPORT`, `assurance_loop=L0`,
`status=CONFIRM` (negative fixture kanıtı eksikse).

> ⛔ **`--schematic-parity` bayrağı olmadan parity kontrolü HİÇ KOŞMAZ** ve JSON
> raporda `schematic_parity: []` döner. Bu "0 sorun" değil, "**kontrol
> yapılmadı**" demektir — ikisi rapordan ayırt edilemez. usb-hs-breakout'ta
> bayraksız koşu `0` gösterirken bayraklı koşu **17 uyumsuzluk** buldu
> (footprint'lerde eksik şematik alanları, TP'lerin Value/FPID/BOM özniteliği,
> şematikte karşılığı olmayan fiducial'lar). Aynı tuzak `--severity-all` için
> de geçerli: dar severity filtresi ipek/DFM sınıflarını tamamen gizler.
> **Bu uyarının kanonik sahibi bu bölümdür**; diğer skill'ler buraya bağ verir.

Parity kapatılırken **susturma değil sınıflandırma** yap: fiducial gibi
şematikte sembolü olmaması *gereken* öğeleri
`FP_BOARD_ONLY | FP_EXCLUDE_FROM_BOM | FP_EXCLUDE_FROM_POS_FILES` işaretle.

> ⚠️ `footprint.SetField(ad, deger)` alanı **varsayılan olarak GÖRÜNÜR** yapar
> ve **F.Silkscreen**'e koyar. Dokümantasyon alanı (MPN, tedarik, dielektrik)
> ekliyorsan hemen ardından `fld.SetVisible(False); fld.SetLayer(pcbnew.F_Fab)`.
> Yapılmazsa 7 footprint × 2–5 alan = ipek üzerinde üst üste binen onlarca
> metin: ölçülen sonuç **186 silk ihlali** (108 overlap + 71 over_copper + 7 edge).

### Netlist okurken iki tuzak
- **Yerel** label'lı net'ler netlist'te sheet yolu önekiyle gelir: `/VBUS`. Global
  label ve power sembolü isimleri öneksizdir → karşılaştırmadan önce normalize et.
- `#PWR*` / `#FLG*` güç sembolleri netlist'te **node olarak görünmez**. Power
  sembolünün bağlandığının kanıtı, net'in o **isimle adlanmasıdır** (`GND`).

### PWR_FLAG'in tek meşru kullanımı
Gerçek **kart-dışı besleme noktasında** (konnektör/batarya terminali, pasif pinli
kaynak), **ray başına en fazla bir**, yorumla belgelenmiş. "Power input not driven"
hatasını rayın gerçekten beslenmediği yerde susturmak = bring-up'ta ölü ray.

## 5. (d) ERC'nin **yapı olarak** yakalayamadıkları

ERC bağlantı topolojisine bakar; değer/yön/zamanlama doğruluğuna bakmaz.
"ERC 0 hata" bir kartın çalışacağını göstermez. Şematik kapanmadan bu listeyi
elle koştur ve sonucu `sematik_riskler.md`'ye yaz:

- **Açık (floating) CMOS girişi.** Kullanılmayan giriş pini ERC'de sorunsuz
  görünür; gerçekte salınır, akım çeker, latch-up'a gider. Her kullanılmayan
  giriş ya pull-up/down ile ya doğrudan rayla sonlanmalı. (Kullanılmayan
  *çıkış* açık bırakılır — girişle karıştırma.)
- **Strap / boot / mode pini.** Değeri yanlışsa kart açılmaz ama ERC temizdir.
  Her strap'ı `TBD` yerine datasheet tablosuyla eşleştir; ayrıca **ölçülebilir**
  yap (TP) ve mümkünse iki seçenekli (DNP) bırak. Satırın alan alan kaynak
  sözleşmesi ve kapsam aritmetiği: **§11**.
- **I²C pull-up'ı bir hesaptır, 4.7k gelenek değil.**
  `Rmin = (VDD − VOL) / IOL` (tipik IOL 3 mA → 3.3 V'ta ~0.5 kΩ alt sınır),
  `Rmax ≈ tr_bütçesi / (0.85 × C_bus)`; Fast-mode tr ≤ 300 ns, C_bus ≤ 400 pF.
  Uzun hat/çok cihaz → C_bus büyür → R küçülmeli. **Her ray/segmentte tek çift**
  pull-up; her cihaza ayrı koyarsan paralel direnç Rmin'in altına düşer.
  Seviye çeviricili (farklı VDD) segmentlerde iki taraf ayrı hesaplanır.
  **Hesabı elle yapma:** `tools/scripts/sch/i2c_pullup.py --netlist … --segments …`
  paralel eşdeğeri netlist'ten çözer ve pencereyi kurar. Yukarıdaki sayıları
  script'e **kaynağıyla** vermek zorundasın (doküman + revizyon); script'in
  içinde hiçbir I²C sabiti yoktur ve kaynaksız girdi PASS değil NO_COVERAGE
  üretir.
- **Kutup/yön hataları:** diyot, ESD dizisi, LED, elektrolitik, akım-algılama
  şöntü. ERC pin tipine bakar, yöne bakmaz.
- **Koruma sırası:** konnektör → ESD → (CMC) → seri eleman → IC. Şematikte sıra
  yanlışsa layout düzeltemez ([[../emc-esd/SKILL|emc-esd]] §ESD sahibidir).
- **Kapasite değerleri DC-bias sonrası** hesaplanmalı
  ([[../guc-isil/SKILL|guc-isil]] §3b); regülatör stabilitesi/PDN buna bağlı,
  ERC'nin haberi yok.
- **Ray enable/sıralama** (PMIC PG → sonraki enable). Yanlış sıra core'u yakar;
  şematikte zincir, bring-up'ta checklist
  ([[../dft-testpoints/SKILL|dft-testpoints]]).
- **Kristal:** yük kapasitesi `C = 2·(CL − C_stray)` (C_stray ~2–5 pF), drive
  level ve negatif direnç marjı. Yanlış CL → frekans kayması / hiç kalkmama.

Her satır **kontrol edildi / TBD(+spec) / CONFIRM**. Boş bırakılan satır
"geçti" sayılmaz.

### Kural kapsamını sorgula: her kural her pine uygulanmaz
"Decoupling ≤1.5 mm" **anahtarlama akımı çeken** IC besleme pini içindir.
Kapsam dışı örneği ve ölçülmüş gerekçesi: [[../_shared/rule-conflicts.md]] §2.
Doğru çıktı, kuralın kapsam dışı olduğunu **ölçümle yazmaktır**.

## 6. (e) Şematik düzenlemesinin PCB'ye yan etkisi

Sembol **alanları** (MPN, Tedarik vb.) parity kontrolüne girer: şematikte MPN'i
doldurmak, board footprint alanları eskide kalınca
`footprint_symbol_field_mismatch` üretir — bu kartta 7 ihlal olarak ölçüldü.
Alanları board tarafına da yaz (`FOOTPRINT.SetField(name, value)`;
`HasFieldByName` **yoktur**, `HasField`/`SetField` vardır) ve parity'yi
**değişiklikten sonra tekrar koş**.

### Cache'lenmiş `lib_symbols` sembolünü script'le değiştirme
`lib_symbol_mismatch` uyarısını, şematikteki cache'lenmiş sembol tanımını
kütüphanedekiyle değiştirerek kapatmaya çalışmak **kartı bozar**: kütüphane
sürümü türetilmiş (`extends`) veya farklı pin/graphic düzeninde olabilir.
Ölçüldü: 7406 baytlık cache → 1451 baytlık kütüphane sembolü ile değiştirilince
ERC **1 → 9 uyarıya** çıktı. Bu bir **GUI adımıdır**
(*Tools → Update Symbols from Library*); script'le yapılacaksa öncesi/sonrası
netlist **kapsamıyla birlikte** karşılaştırılmalı
([[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] §Ölçüm hatası sınıfları).

### ERC JSON'unda koordinat/uzunluk birimi (KiCad 10.0.5 tool-bug)
`kicad-cli sch erc --format json` `coordinate_units: "mm"` yazar ama değerleri
**100'e bölünmüş** verir: gerçek `(82.55, 99.06) mm` → `(0.8255, 0.9906)`;
2.54 mm'lik tel `length 0,0254 mm` görünür. Koordinatı dosyada aramadan önce
×100 yap; aksi halde bulgu "kaynağı bulunamadı" diye açık madde olarak kalır.

## 7. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Floating giriş / strap / kutup / kristal CL | C | `sematik_riskler.md` satır satır; ERC yapısal olarak göremez |
| I²C pull-up penceresi | C→A | `tools/scripts/sch/i2c_pullup.py` **var** ve pull-up ağını netlist'ten çözüyor; VDD/VOL/IOL/C_bus/tr **kaynaklı** girilene kadar sonuç NO_COVERAGE |
| DC-bias sonrası stabilite | C | Üretici eğrisi; [[../guc-isil/SKILL|guc-isil]] §3b |
| Ray sıralama davranışı | D | Bring-up ölçümü |
| Değer/zamanlama doğruluğu | C | ERC topolojiye bakar |
| Netclass↔net **ters yön** kapsaması | C | §10.2 — araç yok, matris satırı assertion'sız; en fazla `CONFIRM` |
| Strap/boot/mode gerçek seviyesi | D | §11.4 — kart üzerinde ölçülür ([[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]]) |

## 8. Doğrulama

```bash
python3 tools/tests/test_sch_wire.py                 # regresyon (KiCad KAPALI)
python3 tools/scripts/sch/rewire.py board.kicad_sch  # dry-run rapor
python3 tools/scripts/gate_receipt.py gate_receipt.json
```

**Kalite kapısı — bu sırayla:**
1. `verify_nets(sch, expected)` → `parts.json`/`rail_tree.json`'dan türetilmiş
   **beklenen from→to net tablosu** ile gerçek netlist birebir tutuyor mu.
   Çizginin ekranda görünmesi bağlantı değildir; **tek kanıt netlist'tir.**
2. `unconnected-*` net sayısı = 0 (her biri unutulmuş bir wire).
3. `erc.json` error = 0 — **PWR_FLAG serpiştirerek susturmak YASAK**.
4. **Hibrit denge:** `(wire` sayısı > 0; etiketle bağlanan net oranı ≤ %40;
   "tel zorunlu" listesindeki net'lerin hiçbiri etiketle bağlanmamış; her etiket
   adı ≥2 yerde geçiyor. Üçünü de o koşuda **kendin say**, script çıktısına güvenme.
5. **Fault-injection:** bir wire ucunu 1.27 mm kaydır → kapı (1) KIRILMALI.
   Kırılmıyorsa test boştur. Dikkat: ucu wire'ın *ortasına* kaydırmak T-bağlantı
   sayılır ve kopukluk yaratmaz; **pin ucundan** kaydır.

## 9. Kabul kriterleri

- [ ] `(wire` sayısı > 0; tamamen etiketle bağlanmış sayfa yok.
- [ ] Etiketle bağlanan net oranı ≤ %40; her etiket adı ≥2 yerde geçiyor.
- [ ] "Tel zorunlu" listesindeki netler (seri elemanlar, diff çiftler, FB ağı,
      kristal, güç/GND) etiketle bağlanmamış.
- [ ] `verify_nets` beklenen from→to tablosuyla birebir tutuyor.
- [ ] `unconnected-*` = 0.
- [ ] `erc.json` error = 0, **PWR_FLAG ile susturma yok**; PWR_FLAG yalnız
      kart-dışı besleme noktasında ve ray başına ≤1.
- [ ] ERC/DRC komutları `--exit-code-violations` **ve** parity için
      `--schematic-parity --severity-all` bayraklarıyla koşuldu; exact argv
      receipt'te.
- [ ] Negative fixture (kasıtlı ERC ihlali) nonzero exit üretti.
- [ ] Fault-injection: pin ucundan 1.27 mm kaydırma kapıyı KIRDI.
- [ ] `sematik_riskler.md` her satırı **kontrol edildi / TBD(+spec) / CONFIRM**;
      boş satır yok. Kapanmamış C maddesi varken Faz 3 açılmadı.
- [ ] Her dış arayüz için `interface_contract.json` dolu; rol açık olmayan
      arayüzde koruma MPN/limit seçilmedi.
- [ ] Tek kanonik rail ismi kullanıldı (`3V3` vs `+3.3V` karışması yok).
- [ ] UUID'ler deterministik (uuid5); script tekrar koşunca diff patlamıyor.
- [ ] Board yazımı öncesi KiCad kapalı + `.bak` alındı; yazım tek sahipli.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.
- [ ] §10: kural taşıyan / dışarıdan adreslenen hiçbir net üretilmiş adla
      bırakılmadı; `eligible` evreni netlistten değil **kısıt kayıtlarından**
      kuruldu; ters yön verdict'i ya kanıtıyla ya `CONFIRM` olarak yazıldı.
- [ ] §11: her strap/boot/mode/clock-select/reset satırı `strap_binding.json`'da
      ve her satırın kaynağı `document_id` + `revision` + `locator` + `accessed`
      + `snapshot_sha256` beşlisiyle dolu; kaynaksız satır PASS sayılmadı.

## 10. Kararlı semantik net kimliği — netclass↔net bağının İKİ yönü

> **Bu bölüm net kimliğinin kanonik sahibidir.** Net adını şematiği üreten
> taraf verir; ayrı bir sahip açmak şematik otoritesini bölerdi
> ([[../_shared/rule-conflicts.md]] §9). Burada sahiplenilen şey **kimlik ve
> bağın kendisi**dir — kısıtın *değeri* değil. 100 Ω hedefi
> [[../empedans-stackup/SKILL|empedans-stackup]]'ın, netclass geometrisi
> [[../highspeed-routing/SKILL|highspeed-routing]] / [[../dfm/SKILL|dfm]]'in
> kalır. Bu skill "kural bağlanabiliyor mu" sorusunun sahibidir, "kural doğru mu"
> sorusunun değil.

Repo'nun kendi ifadesi (`block-catalog` önkoşulu,
`tools/scripts/sync_tool_registry.py`):

> **Üretilmiş adla VAR OLAN bir net, kural bağlama amacıyla YOKTUR** — ve adı
> kararsızdır.
> (*"A net that EXISTS with a generated name is ABSENT for rule-binding
> purposes — and its name is unstable"*)

Ölçülmüş vaka: CAM-W'de CSI-2'nin **on iletkeni vardı, adı yoktu**
(`Net-(D101-IO1)` … `Net-(D103-IO2)`); netlistte yalnız 11 adlandırılmış net
bulunuyordu ve CSI çiftlerinin hiçbiri o listede değildi. Kartın en önemli
elektriksel kısıtı — 100 Ω ±%20 diferansiyel — tasarımcının verdiği bir ada
değil ESD diyotunun referansına asılı kalmıştı; D101 yeniden numaralansa on
netin adı birden değişir ve **o ada bağlı her kural hata vermeden çözülür**.
Tam kayıt: [[../../docs/BULGU-isimsiz-diferansiyel-netler.md]] §1, §2.

"Adı kim verdi" gerekli ama **yeterli değildir**: PROC-A'da adı araç verdi *ve*
kural o ada bağlandı (`CML_DIAG` → `Net-(U201B-CMLOUT?)`). Yalnız "bağlanacak
ad yok" diyen bir denetleyici o kartı temiz sanar (aynı belge §4.1).

### 10.1 Kararlı semantik kimliğin tanımı (beş yordam)

Bir net, onu tüketen kısıt/sözleşme/test sistemi için kararlı semantik kimliğe
sahiptir **ancak ve ancak** beşi birden doğruysa:

1. kanonik ad netlistte **çıplak** ve **tam** olarak var (sheet öneki
   normalize edildikten sonra — §4 "Netlist okurken iki tuzak"),
2. beklenen düğüm kümesi o nette,
3. netclass selector'ı **üretilmiş** bir string değil,
4. selector **sıfır net** eşlemiyor (sessiz çözülmenin imzası),
5. selector'ın eşlediği küme beklenen kümenin **tam aynısı** — "en az bir
   eşleşme" yetmez.

Kanonik ad tek kaynaktan gelir: `interface_contract.json` / `rail_tree.json` /
karar kaydı. Üçüncü bir lehçe üretmek yasaktır (§2 "Etiket adı kanonik ve tek").
Düzeltme **üreteçte** yapılır; üretilmiş bir `.kicad_sch`'i elle düzenlemek
yanlış katmandır — bir sonraki koşu değişikliği iz bırakmadan siler.

**Bir tele etiket koymak net sayısını DEĞİŞTİRMEZ.** CAM-W'de ölçüldü:
düzeltmeden önce ve sonra 38 net, düğüm kümeleri harfi harfine aynı; değişen
tek şey `generated` 10 → 0. Net sayısı sabitine bakan bir fikstür bu yüzden
güncellenmez — güncellenirse gerçek bir topoloji bozukluğu teşvik edilir.

### 10.2 Bağın iki yönü — hangisi otomatik, hangisi elle

| Yön | Soru | Bugün ölçen | Durum |
|---|---|---|---|
| **İleri** (kısıt → net) | Beyan edilen her kısıt kararlı-adlı, tekil ve doğru düğüm kümeli bir nete bağlanıyor mu | `block-catalog` (`--decision … --netlist …`), üç değerli: `PRESENT` / `ABSENT` / `UNVERIFIABLE`; `missing_nets` + `generated_name_nets` ayrı yayınlar | **otomatik** — ama bir *karar kaydı* ister; netlist yoksa `NO_COVERAGE`, "unverifiable" asla "present"e katlanmaz |
| **Ters** (net → kısıt) | Kısıt taşıması gereken her net gerçekten bir kısıt taşıyor mu | `dfm-emc-check::hs_netclass_assigned` — **yalnız adı `hs_regex`'e uyan** board netleri, "Default dışında bir netclass'ta mı" | **kısmi**; evreni netlistteki *adlar* tanımlar |
| **Aşırı kapsama** (desen → yanlış net) | Desen, kısıt gerektirmeyen güç/CC netlerini yanlışlıkla kapsıyor mu | yok | **elle** — `dfm_emc_check.py` bunu kendi metninde yazıyor: "ters yön … elle gözden geçirilir" |

Ters yönün sessiz deliği tam olarak burasıdır ve `scanned=0` kapısı onu
kapatmaz. `common/findings.py::result` sayesinde `scanned=0` **zorunlu**
`NO_COVERAGE`'dır (`Finding` `scanned=0` ile `PASS` taşıyamaz, `ValueError`
atar). Ama CAM-W vakasında `scanned` sıfır olmaz: adı kaybolmuş on CSI neti
hiçbir desene uymaz, **`eligible`'a hiç girmez**, kontrol geri kalanlar
üzerinde `PASS` verir. Kırpılmış bir evren, boş bir evrenden çok daha
sessizdir.

Matrisin kendi satırı da bunu hâlâ böyle yazıyor —
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] §2 Faz 2:
"Netclass↔net iki yönlü kapsama | A | `dfm_emc_check.py::hs_netclass_assigned`
(**+ ters yön elle**)".

Ölçüm (2026-08-10, bu repoda koşuldu):

```bash
python3 tools/scripts/assert_matrix.py \
  --set tools/fixtures/assertions/matrix_core.json \
  --matrix skills/dogrulama-matrisi/SKILL.md
# rules_asserted: NO_COVERAGE — 48 matris satırının 11'i assertion'lı,
# 37'si UNASSERTED_RULE. "Netclass↔net iki yönlü kapsama" o 37'nin içinde.
```

Yani ters yön için bugün ne bir araç ne bir assertion var. **GREEN yolu
yoktur.** Assertion seti `dogrulama-matrisi`'nin sahipliğindedir; bu skill onu
yazmaz, ölçümünü okur ve talep eder.

### 10.3 Kapsam aritmetiği

Ortak kural: [[../_shared/coverage-gate.md]].

- **`eligible`** = kural taşıyan **ya da dışarıdan adreslenen** her net.
  Evren **netlistin adlarından değil, kısıt kayıtlarından** kurulur —
  `interface_contract.json`'daki her pin/çift, `rail_tree.json`'daki her ray,
  netclass tablosunun her üyesi, `.kicad_dru` kurallarının her hedefi,
  uzunluk-eşleme/skew gruplarının her üyesi. Bu tek karar, adı kaybolmuş neti
  evrende tutan şeydir; netlist-tabanlı bir evren onu tanım gereği kaçırır.
- **`scanned`** = §10.1'deki beş yordamın **beşi de** koşulan net.
- **`excluded`** = yalnız gerekçeli: `no_connect_nets()` ile beyan edilmiş
  kasıtlı no-connect'ler ve **hiçbir dış tüketicisi olmayan** generated
  power-stub'lar. Kusur olan, kural taşıyan/dışarıdan adreslenen generated
  nettir; ikisini aynı listede raporlamak CAM-W'de 10 gerçek vakayı 18
  zararsızın altına gömüyordu. Her `excluded` satırı `uncovered_reason` ile
  yazılır; gerekçesiz exclusion `INVALID_COVERAGE`/`RED`.
- `scanned + excluded == eligible`; tutmuyorsa `INVALID_COVERAGE`/`RED`.
- **`scanned = 0` → `NO_COVERAGE`**, asla `GREEN` değil.

Bugünkü en iyi verdict: ileri yön kanıtlıysa ve ters yön elle koşulmuşsa
**`CONFIRM`**. `eligible` evreni kısıt kayıtlarından kurulmamışsa
**`NO_COVERAGE`** — `hs_netclass_assigned`'ın PASS'ini ters yönün kanıtı diye
yükseltmek yasaktır.

Kapatan üç şey (açık madde olarak `acik_maddeler.md`'ye yazılır):

1. `eligible`'ı kısıt kayıtlarından kuran bir üretici,
2. §10.1'deki beş yordamın repo genelinde makine karşılığı — bugün yalnız kart
   üretecinde var (`cam-w-og05b10/scripts/verify.py [13]`), `pcb-agent`
   tarafında **yok**,
3. matris satırına bir assertion + `--prove` fault-injection (sahibi
   `dogrulama-matrisi`).

> **Açık soru / varsayım** — ters yönü bugün tek başına kapatan bir araç
> olmadığı için bu bölüm muhafazakâr seçimi yapar: ters yön verdict'i en fazla
> `CONFIRM`'dür ve kanıtı elle koşulmuş bir tarama kaydıdır. Yukarıdaki üçlü
> kapanmadan bu satır `GREEN` yazılamaz. Ayrıca `hs_regex`'in doğru evreni
> tanımladığı **varsayımı sınanmamıştır**; deseni kimin, hangi kayda bakarak
> yazdığı `uncovered_reason` alanına yazılır.

### 10.4 Bu bölümün kanıtlayamadıkları

| Konu | Seviye | Neden |
|---|---|---|
| Kısıtın **değeri** doğru mu (100 Ω, sınıf geometrisi) | C→D | Kimlik burada, limit [[../empedans-stackup/SKILL|empedans-stackup]]'ta; kapanış coupon/TDR (D/L3) |
| İki ayrı tel güdüğü + **aynı etiket** | C | Netlist tek net, doğru ad, doğru düğüm sayısı gösterir; hiçbir netlist kapısı ayırt edemez. Kapı etiketin yokluğu değil **fiziksel tel yolunun varlığı**dır (birleşim-bul grafı); `pcb-agent` tarafında böyle bir araç yok → `NO_COVERAGE` |
| Adın zaman içinde kararlı kaldığı | C | Refdes yeniden numaralanınca üretilmiş ad değişir; ancak iki koşu arası diff ile görülür (`STALE`) |
| Ters yönün tam kapsaması | C | §10.2/§10.3 — bugün araçsız |

## 11. Strap / boot / mode / clock-select / reset kutbu — KAYNAK SÖZLEŞMESİ

§5 bu riski zaten sahipleniyor ("Değeri yanlışsa kart açılmaz ama ERC
temizdir") ve karar satırlarını yazıyor. Eksik olan bir sahip değil,
**otomatik bir kaynak sözleşmesiydi**: bu bölüm onu tanımlar. Ayrı bir skill
açmak kablolama kararına ikinci bir otorite getirirdi
([[../_shared/rule-conflicts.md]] §9).

### 11.1 ERC bunu neden **yapı olarak** yakalayamaz

ERC **bağlantıyı** kanıtlar, **seviyeyi** değil. `BOOT_SEL` pini 10 kΩ ile
GND'ye de 3V3'e de çekilmiş olsa ERC her iki şemada da temizdir: iki durumda da
pin sürülüyor, çakışma yok, floating giriş yok. İkisini ayıran tek şey
üreticinin konfigürasyon tablosudur ve o tablo netlistte **hiç yoktur**. Aynısı
reset kutbu (active-low ↔ active-high), clock kaynağı seçimi (dahili ↔ harici),
adres/mode strap'ları için geçerlidir. Bu yüzden kanıt netlistten değil
**kaynaktan** gelmek zorundadır.

### 11.2 Sözleşme — her satırın taşıması gerekenler

Makine-okur iskelet:
[`references/strap_binding.schema.json`](references/strap_binding.schema.json)
(`strap-binding/v1`), çıktı `strap_binding.json`.

| Alan | Anlamı | Boşsa |
|---|---|---|
| `refdes` + `pin` + `net` | Hangi fiziksel pin, hangi net | satır taranmış sayılmaz |
| `function` | `boot` / `mode` / `clock_select` / `reset_polarity` / `address_select` / `config` | `unknown` → `CONFIRM` |
| `intended_level` | `high` / `low` / `float` / `analog` | `unknown` → `CONFIRM` |
| `resolved_by` | `pull_up` / `pull_down` / `hard_tie_rail` / `hard_tie_gnd` / `dnp_pair` / `driven_by_host` | `unresolved` → `CONFIRM` |
| `source` | **`AUTHORITATIVE_SOURCE` beşlisi**: `document_id` + `revision` + `locator` + `accessed` + `snapshot_sha256` ([[../_shared/coverage-gate.md]] §`evidence_origin`) | `CONFIRM` — asla PASS |
| `measurable` | Ölçüm noktası (TP) var mı; tek kapanış yolu ölçümdür | `CONFIRM` |

`revision` ve `accessed` isteğe bağlı değildir: revizyonsuz bir üretici belgesi
bir belgeyi değil bir **belge ailesini** adlandırır (repo'nun `model-registry`
önkoşulundaki ifade: *"a vendor document without a revision and a date names a
FAMILY of documents"*). `snapshot_sha256` aynı işi bayt düzeyinde yapar: altına
başka bir PDF konmuş bir `document_id`, değiştirdiği belgenin otoritesini
taşımaya devam ederdi.

**Repo varsayılanından çözülmüş bir strap `LOCAL_POLICY`'dir ve PASS değildir.**
Ortak kapı bunu zaten söylüyor: "Kaynaksız yerel fallback yalnız
`LOCAL_POLICY` / `SCREEN_ONLY` üretir." "Bu ailede genelde pull-down olur" bir
kaynak değildir. `LOCAL_POLICY` bir satırı kapatmaz, yalnız kimin neyi
varsaydığını görünür kılar.

Mümkün olan her strap **iki seçenekli (DNP ikili)** bırakılır ve **TP ile
ölçülebilir** yapılır (§5). Bu bir konfor değil kapanış koşuludur: satırın
gerçek seviyesi ancak kart üzerinde ölçülünce kanıtlanır.

### 11.3 Kapsam aritmetiği

- **`eligible`** = tasarımdaki **her** strap/boot/mode/clock-select/
  reset-polarity/config neti. Evren netlistten türetilemez — hangi pinin strap
  olduğu **paket/datasheet bilgisidir**, iletkenlikte görünmez. Bu yüzden
  `eligible` bir **beyandır** ve beyanın kendisi `parts.json`'daki her aktif
  parçanın konfigürasyon pin tablosuna dayanır; tablosu okunmamış bir parça
  `eligible`'a katkı yapamaz ve bu bir `CONFIRM` satırıdır, sıfır değil.
- **`scanned`** = §11.2'deki altı alanın **altısı da** dolu olan satır.
- **`excluded`** = yalnız gerekçeli: parça o modda kullanılmıyor, ya da pin
  datasheet'te NC beyan edilmiş. Gerekçesiz exclusion `INVALID_COVERAGE`/`RED`.
- `scanned + excluded == eligible`.
- **`scanned = 0` → `NO_COVERAGE`.**

Verdict eşlemesi:

| Durum | Verdict |
|---|---|
| Kaynaklı, seviye eşleşiyor, ölçülebilir | Bu skill'in verebileceği en fazlası: **kâğıt üzerinde doğru**. Fiziksel seviye D/L3'te kapanır |
| Kaynak var, seviye datasheet tablosuyla **çelişiyor** | `RED` |
| Kaynaksız / repo varsayılanından çözülmüş | `LOCAL_POLICY` — PASS değil |
| Satır var, alanları eksik | `CONFIRM` |
| Strap ailesi hiç satırlanmamış | `NO_COVERAGE` |

**Sessiz geçiş yasaktır:** kaynaksız bir strap "ERC temiz" diye geçmez; §5'in
"Boş bırakılan satır 'geçti' sayılmaz" kuralı bu bölümün alan alan hâlidir.

### 11.4 Bu bölümün kanıtlayamadıkları

| Konu | Seviye | Neden |
|---|---|---|
| Gerçek boot/mode seviyesi | D | Kart üzerinde ölçülür; üreticisi [[../l3-bringup-karakterizasyon/SKILL|l3-bringup-karakterizasyon]] |
| Reset kutbunun **davranışı** (kalkış sırası) | D | Bring-up ölçümü; şematikte zincir, kartta checklist ([[../dft-testpoints/SKILL|dft-testpoints]]) |
| Datasheet tablosunun kendisinin doğruluğu | C | Erratum/revizyon; `revision` + `snapshot_sha256` yalnız *hangi* belgeye baktığımızı sabitler, belgenin doğru olduğunu değil |
| Strap'ın layout'ta doğru raya bağlandığı | B | Netlist + `--schematic-parity` (§4); bu bölümün konusu değer, o bölümün konusu bağlantı |

## Kurallar (kısa)

- Üretim hataları auto-fix ile maskelenmez; ERC yakalar.
- Raporladığın metriği (`(wire` sayısı, net sayısı, ERC error) **o koşuda kendin ölç**.

## İlgili

[[../dft-testpoints/SKILL|dft-testpoints]] · [[../emc-esd/SKILL|emc-esd]] ·
[[../guc-isil/SKILL|guc-isil]] · [[../konnektor/SKILL|konnektor]] ·
[[../arayuz-eslesme-kapisi/SKILL|arayuz-eslesme-kapisi]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]] ·
[[../pcb-orchestrator/SKILL|orchestrator]]

## Araç kancaları

> Ad listesine eklemek tek başına tetiklenebilirlik sağlamaz; aşağıdaki satır
> aracın *ne zaman* çağrılacağını söyler.

- **`hand-solder-gate`** — Yalnız **el-lehim / perfboard / prototip** şematiği
  çizerken, wire kapısının bu skill'e özgü sürümü olarak koşulur. Bu skill'in
  temel iddiası ("bağlantının kanıtı netlist'tir, çizginin görünmesi değil")
  el-lehim bağlamında **tersine döner**: kartı lehimleyen kişi netlist okumaz,
  ekrana bakar. O yüzden orada etiket telin **yerine geçemez**.
  Kapı iki seviye ölçer ve hüküm ikincisindedir: **L1** her etiketin bir telin
  ucunda/üzerinde durup durmadığı, **L2** bir netin bütün pinlerinin **tek** bir
  tel-bağlı bileşende olup olmadığı. L1'i tek başına kapı saymak tuzaktır —
  sayfanın iki ucunda, her biri kendi 2.54 mm'lik stub'ının ucunda duran iki
  ayrı `FSYNC` etiketi L1'i geçer ama aralarında tel **yoktur**; netlist onları
  aynı net sayar, KiCad memnundur, lehimleyen kişi birleştirici hiçbir şey
  görmez.
  **Fab için çizilen hiyerarşik / net-label'lı şematiklere UYGULANMAZ**; klasör
  adı el-lehime benzemiyorsa araç koşmayı reddeder (`--force` bunu ezer).
  Güç rayları varsayılan olarak muaftır (güç sembolü + kısa stub yeterli),
  `--strict` bu muafiyeti kaldırır.
  **Çıkış kodları bu repodaki 0/1/2 sözleşmesinden SAPAR:** `0` GREEN, `2` RED,
  `3` girdi eksik. `2`'yi "araç cevap üretemedi" diye okuyan bir çağıran,
  gerçek bir ihlali ortam hatası sanır.
