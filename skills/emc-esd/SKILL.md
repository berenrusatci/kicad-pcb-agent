---
name: emc-esd
description: EMC/ESD ön-uyum — hedef standart/sınıf/mesafe, CM akımı ve kablo antenleri, dönüş yolu, stitching via, decoupling PDN, IEC 61000-4-2 koruma zinciri. Yaygın "EMC kuralları"nın hangileri ölçümle yanlışlandı.
version: 1.0.0
last-reviewed: 2026-08-05
phase: "Faz 2 (topoloji: stackup + koruma zinciri) + Faz 3 (yerleşim/routing uygulaması)"
assurance_loop: [L1, L3]
evidence_origin: TOOL_REPORT
signoff: false
inputs:
  - {artifact: stackup.json, schema: ../empedans-stackup/references/stackup.schema.json, required: true}
  - {artifact: rail_tree.json, schema: ../../tools/schemas/rail_tree.schema.json, required: true}
  - {artifact: interface_contract.json, schema: ../sematik-erc/references/interface_contract.schema.json, required: true}
  - {artifact: phy_contract.json, schema: ../highspeed-routing/references/phy_contract.schema.json, required: true}
outputs:
  - {artifact: esd_chain.json, schema: references/esd_chain.schema.json}
  - {artifact: return_via_rules.json, schema: references/return_via_rules.schema.json}
  - {artifact: emc_plan.md, schema: null}
  - {artifact: emc_geometry_receipt.json, schema: null}
  - {artifact: precompliance_notes.md, schema: null}
writes_board: false
tools: [dfm-emc-check, stitch-density, emc-rules, decoupling-coverage, reference-plane, topology-advise]
---

# SKILL: EMC / ESD Ön-Uyum

Ortak kapı kuralları: [[../_shared/coverage-gate.md]] ·
`f_knee_edge` vs `f_nyquist`: [[../_shared/glossary.md]] §1 ·
Kural çatışmaları: [[../_shared/rule-conflicts.md]] §1, §6

> EMC bir "son kontrol" değil, **stackup + yerleşim kararıdır**. Faz 4'te
> keşfedilen EMC problemi kart dönüşü demektir; bu skill Faz 2–3'te koşar.

## 0. Ne zaman çağrılır

- **Faz 2 (9a — topoloji):** interface + connector kontratından koruma zincirini
  dondurur. **Şematik 9a tamamlanmadan açılmaz.**
- **Faz 3 (9b — fiziksel):** konnektör tarafı koruma sırası, return path,
  stitching ve DNP filtre alanını routed board üzerinde ölçer.
- Kart-dışına çıkan **her** arayüz için; yeni bir arayüz eklendiğinde yeniden.

## 1. Önce hedefi yaz: hangi standart, hangi sınıf, hangi mesafe

Limitsiz EMC çalışması ölçülemez. `emc_plan.md` ilk satırları: hedef standart +
**baskı** + ürün sınıfı + ölçüm mesafesi + dedektör.
- Referans (ITE/multimedya, Class B — konut): 30–230 MHz **30 dBµV/m @10 m**,
  230–1000 MHz **37 dBµV/m @10 m**; 3 m'de +10 dB (40 / 47), quasi-peak.
  Üst ölçüm frekansı EUT'nin en yüksek dahili frekansından türetilir — 1 GHz'te
  otomatik durma.
- `CONFIRM: satış ülkesinde yürürlükteki CISPR 32 / EN 55032 baskısı, ürün
  sınıfı ve test sahasının izin verdiği mesafe.` Sınıfı sen seçmezsin, pazar seçer.

**Bağışıklık, ayrı ve port-bazlı bir listedir.** IEC 61000-4-x *temel*
standartlardır: seviyeyi **ürün standardı** seçer, sen değil. Her dış port için
hangi olayın hangi layout kararına döndüğünü yaz:

| Test | Olay | Layout karşılığı |
|---|---|---|
| -4-2 | ESD | şaseye kısa boşalma yolu + TVS zinciri (§6) |
| -4-3 | Işınan RF | kesintisiz referans, hassas düğümde küçük döngü alanı |
| -4-4 | EFT/burst | besleme/sinyal/kontrol portunda **konnektör tarafı** filtre alanı |
| -4-5 | Surge | yalnız uygulanabilir dış hatlarda enerji dereceli koruma + creepage |
| -4-6 | İletilen RF | kablo ortak-mod sonlandırma/filtre |

`CONFIRM: ürün standardı, port türü, kablo uzunluğu, performans kriteri A/B/C.`

Vault'taki `4/8 kV kontakt` hedefi yalnız iş akışını başlatan
`LOCAL_POLICY` varsayılanıdır; otorite veya D/L3 PASS değeri değildir. Gerçek
severity'yi seçilen ürün standardı ve
[`references/esd_test_profile.schema.json`](references/esd_test_profile.schema.json)
belirler. Profil yoksa ön-elek ilerler fakat verdict `SCREEN_ONLY/CONFIRM`'den
PASS'e yükselmez. `evidence_origin=LOCAL_POLICY`, `assurance_loop=L1+L3`,
`status=LOCAL_POLICY`.

## 2. Işıyan şey kart değil, **kablodur**

Radyasyonlu emisyon arızalarının **%80–90'ı ortak mod (CM)** kaynaklıdır ve
gerçek anten kart değil, karta bağlı kablolardır (tipik kablo rezonansı
30–100 MHz — CISPR/FCC bandının tam ortası). Büyüklük sezgiye aykırıdır:
**~5 µA CM akımı, 300 MHz'te 1 m kabloda FCC Class B'yi aşar.**
- Bu yüzden emisyon kontrolü **konnektörde** yapılır, izin ortasında değil:
  CM choke, filtre, shell/şase bağlantısı, konnektör altında kesintisiz GND.
- Kart-dışına çıkan her arayüzü listele (`emc_plan.md`): kablo var mı, uzunluğu,
  hangi sinyal hızları, filtre yeri **yerleşimde ayrılmış mı**.
- Filtre/choke ayak izini **baştan koy** (gerekmezse 0 Ω/DNP). Sonradan yer
  bulunmaz.

### CM choke seçimi: "100 Ω @100 MHz" bir seçim ölçütü değildir
Fark hattına takılan yanlış CMC linki bozar (D-PHY'de hem LP hem HS durumu var).
CMC **DNP varsayılanlı opsiyon** olarak yerleştirilir, seçimi karma-mod
S-parametresiyle yapılır: sorunlu CM bandında yüksek **Scc21** kaybı, link
bandında düşük **Sdd21** kaybı, hat empedansına uygun TDR. Üreticiler MIPI
D-PHY için data-rate bazlı seçim tablosu yayımlar — önce oraya bak. Doğrulama
yerleşimden sonra: CMC takılı/takısız **TDR + göz maskesi** karşılaştırması.
Problem frekansı ölçülmeden parça seçme.

### Ferrit boncuk: rated current filtre değil **sıcaklık** sınırıdır
İki ayrı tuzak, ikisi de sessiz:
- **DC bias**: boncuğun empedansı DC akımla çöker ve bu, rated current'ın çok
  altında başlar. Sıfır-bias `Z@100 MHz` ile seçim yapma. BOM kaydına
  `Z(f)` eğrisi + **bias altındaki** Z/L + DCR + akım sınırı yaz.
- **LC rezonansı**: boncuk + yük MLCC'si bir LC tankıdır; rail'de **peaking**
  yapar (filtre değil, yükselteç). Boncuk+kapasitör ağını kaynağın çıkış
  empedansıyla, hafif **ve** ağır yükte AC-simüle et; peaking varsa seri-R'li
  sönüm kondansatörü ekle. Sönümü ölçmeden boncuk koyma.

## 3. Dönüş yolu tek en büyük kaldıraç

Yarık/void üzerinden geçen hat hem SI hem EMC arızasıdır; düzlemdeki yarık
(slot) yarık-anten gibi ışır. Düzlemi kesme; kesmek zorundaysan hattı üzerinden
geçirme. Kuralın kanonik sahibi ve `100 nF` etiketi:
[[../empedans-stackup/SKILL|empedans-stackup]] §Referans düzlemi.
Ölçüm: `check_reference_plane_continuity()`
([[../highspeed-routing/SKILL|highspeed-routing]] §3.7).

## 4. Stitching via: **iki ayrı kural**, tek eşikle karıştırma

> **Bu bölüm dikiş (stitching) kuralının kanonik sahibidir.**

Bunlar farklı fiziktir ve tek bir `2.5 mm`/`λ/20` sayısına indirgenemez.
`dfm_emc_check.py --stitch-max-mm` tek eşiği tam olarak bu hatayı yapıyor →
eşiği `return_via_rules.json` ile **netclass/arayüz bazlı** ver
([`references/return_via_rules.schema.json`](references/return_via_rules.schema.json)).

**(1) Lokal dönüş via'sı** — sinyal via'sının katman/referans değiştirdiği yer.
Ölçülecek şey "yakında GND via var mı" değil, **giriş ve çıkış referans
düzlemlerini bağlayan** via'nın mesafesidir. Eşik arayüzün/paketin kılavuzundan
gelir, evrenselleştirilmez. Yayımlanmış örnekler ne kadar dağıldığını gösterir:
NXP i.MX 93 referans değişiminde ≤**1.27 mm** (50 mil); TI TUSB564-Q1 simetrik
ve ≤**5.08 mm** (200 mil); PCIe için tipik ~100 mil önerisi. Ortak kural tek:
*closer is better*. Diferansiyel geçişte P/N'ye **simetrik GSSG** geometrisini
ayrıca doğrula (mode conversion).

**(2) Kenar / guard / shield via çiti** — düzlem çifti kenarı, RF bölgesi sınırı.
Burada λ/20 (λ/10 gevşek üst sınır) **başlangıç tahminidir**, PASS/FAIL kuralı
değil. Frekansı saat hızından değil **kenar hızından** al → **`f_knee_edge`**
kullan, `f_nyquist` değil ([[../_shared/glossary.md]] §1). Normatif bir
temel standart yok; kritikse 3B EM ya da ölçümle doğrula.
`CONFIRM: hedef CM frekans bandı ve εeff.`

- Kural değil, eşik kaynağı yaz: her iki sayı da `emc_plan.md`'de **nereden
  geldiğiyle** birlikte durmalı. Kaynağı olmayan eşik `NO_COVERAGE` üretsin.

## 5. Frekans içeriğini saat değil **kenar hızı** belirler

`f_knee_edge ≈ 0.35 / t_r`. 100 MHz saat 500 ps kenarla 700 MHz'e kadar enerji taşır.
- EMC/stitching/filtre hesaplarında `f_knee_edge` kullan, saat frekansını değil.
  Kanal bant genişliği hesapları (via stub, S-parametre) **`f_nyquist`** ile
  yapılır — ikisi ayrı büyüklüktür ([[../_shared/glossary.md]] §1).
- En ucuz çözüm çoğu zaman **kenarı yavaşlatmaktır**: sürücü drive-strength
  ayarı, kritik olmayan hatta seri 22–33 Ω. Bunu Faz 2'de şematiğe koy.
- Yayılı spektrum (SSC) emisyon tepesini düşürür ama **her protokolde serbest
  değildir** (USB host/bazı seri linkler kısıtlar) — kullanmadan önce protokolü
  doğrula, `CONFIRM` yaz.

## 6. Ölçümle yanlışlanmış "kurallar" — uygulama

- **20-H kuralı: emisyon azaltmak için kullanma.** Shim & Hubing (UMR, EMC 2001)
  analitik + EMAP5 modeli + anekoik oda ölçümüyle inceledi; sonuç net:
  *"20-H rule DOES NOT decrease radiated emissions."* Sayıları **karıştırma** —
  hepsi farklı türden:
  | Sayı | Türü |
  |---|---|
  | 3.6 dB (TM10) / 4.4 dB (TM01) | **analitik** maks. alan oranı (Denk. 12–13) |
  | ~2–3 dB | **model** (EMAP5), rezonansta toplam ışıyan güç |
  | ~7 dB (TM10) / **<1 dB** (TM01) | **ölçüm** tepe farkı |

  Ölçümün modelle uyuşmaması makalede kabul edilir (anten faktörü, oda tepkisi,
  sonlu GND düzlemi). Taşınabilir sonuç yön/moda göre değişen bir dB değeri
  değil, **"azaltmıyor, biraz artırıyor"**dur.
  İki nüansı da yaz, yoksa iddiayı fazla gereriz: (1) artış küçüktür ve yalnız
  güç-bus ışıması **baskın** gürültü kaynağıysa anlamlıdır; (2) 20-H'nin kenarda
  **yakın alanı** çevrelediği doğrudur — komşu kart/kablo/kutuya kuplaj
  problemin ise ayrı değerlendirilir. Yerine (emisyon için): düzlem çifti
  kenarını stitching via çitiyle kapat ve PDN empedansını düşür — bunun bu
  makalede kanıtlanmış bir *ikame* olduğunu iddia etme, ayrı gerekçedir.

- **"Üç farklı değer decoupling": geçersiz.** Modern MLCC'de 10 µF ile 100 nF
  **aynı ESL**'ye sahiptir → yüksek frekans empedansları eşittir; farklı değerleri
  paralellemek kendi öz-rezonansları arasında **anti-rezonans tepeleri** yaratır.
  Doğru yaklaşım: **aynı değerden çok sayıda**, mümkün olan **en düşük döngü
  endüktansıyla**, hedef empedans (`Z_target = ΔV / ΔI`) profili düz olacak şekilde.
  **Ama bu da bir slogan olarak uygulanmaz:** `Z_target` sadece başlangıç hedefi.
  Kararı VRM çıkış empedansı + bulk + MLCC + montaj via/düzlem + paket/on-die
  yolunu **birleştiren** bir empedans taramasıyla ver; çalışma bandındaki
  rezonans/anti-rezonans tepelerini limit altında tut. Tek değer mi karışık değer
  mi sorusunu model + ölçüm cevaplar. Her MLCC için üreticinin `Ceff(V,T,yaş)`
  eğrisi ve ESL/S-parametre modeli gerekir; model yoksa değer `TBD`
  ([[../guc-isil/SKILL|guc-isil]] §3b).

- **Kondansatör değeri değil, montaj endüktansı belirler.** 5 mm ötede
  via-in-pad'li 0201, 1 mm yanında ama uzun izle uzak via'ya bağlı 0603'ten
  iyidir. Ölç: pad→via→düzlem döngü alanı. `≤1.5 mm` decoupling mesafesi bu
  döngünün *bir* bileşenidir, tek başına yeterli değil ve yalnız placement
  `SCREEN_ONLY` ön-eleğidir ([[../_shared/rule-conflicts.md]] §2).

- **"Her besleme pinine 100 nF" bir muhasebe kuralıdır, bir hesap değil.**
  Sayıyı PDN hedef empedansından türet; datasheet'in PDN tablosu varsa o kazanır.

## 7. ESD: IEC 61000-4-2 ≠ HBM

İki üreteç arasındaki fark ve neden karşılaştırılamadıkları:
[[../_shared/glossary.md]] §5.

Baskıyı yaz: güncel temel standart **IEC 61000-4-2:2025 (Ed. 3.0, 2025-03-07)**;
2008 baskısını (Ed. 2) iptal etti — eski baskıya atıf yapan metinleri güncelle.
Seviye tablosu (kontakt / hava): L1 2/2, L2 4/4, L3 6/8, L4 8/15 kV. Hava
testinde belirtilen seviyeye kadar **tüm** seviyeler uygulanır. Bu tablo bir
ürün hedefi **değildir** — seviyeyi ürün standardı seçer (§1).

IC'nin "2 kV HBM" etiketi, konnektöre gelen sistem seviyesi darbeye karşı
**hiçbir şey söylemez**. Kural:
1. Zincir: **konnektör → TVS → (çok kısa) GND → seri eleman → IC**. TVS,
   korunacak hattın *konnektör tarafında*, IC'den önce.
2. Seçim ölçütü tepe gerilim değil **gerçek akımdaki clamping**: `V_clamp @ I_PP`
   (kontakt deşarj için ~16 A) < IC'nin `abs max`'ı. Sadece `V_BR` bakma.
   Bu bir *eleme* kriteridir, **geçti belgesi değil**: IEC darbesinde sonucu
   yerleşim paraziti, TVS'in dinamik direnci ve kalan enerji belirler; `abs max`
   çoğu zaman bir IEC darbe dayanım garantisi değildir. TVS kaydına `V_RWM`,
   IEC seviyesi, **TLP/dinamik direnç** veya üreticinin IEC dalga şekliyle
   ölçülmüş clamp verisi, tek-hat C, paket ve S-parametre modelini yaz.
3. **Arızaların çoğu TVS'in kendisinde değil, GND dönüşünün endüktansındadır.**
   TVS GND'si düzleme en kısa yoldan, tercihen kendi via'sıyla (birden fazla).
   Uzun bir GND izi 30 A / 1 ns'de yüzlerce volt üretir → koruma çalışmaz.
   Via-in-pad DFM kuralıyla çatışma: [[../_shared/rule-conflicts.md]] §1
   (**ESD dönüş yolu kazanır**).
4. Kapasitans bütçesi: yüksek hızlı hatlarda TVS kapasitansı linki öldürebilir —
   koruma çalışır, link çalışmaz. Ama **`C < 0.5 pF` evrensel bir limit değil**;
   sınırı arayüzün kendi kanal/SI bütçesinden türet. Çok hızlı arayüzlerde
   0.1–0.45 pF sınıfı adaylarla başla, sonra kanalla doğrula.
   Fiziksel sıra **akış-üzeri (flow-through), stub'sız**: konnektör → TVS →
   kalan kanal → IC. TVS'i hattın yanına saplama pad ile asma.
5. `esd_chain.json`: her dış arayüz için sıra + parça + `V_clamp@I_PP` + hat
   kapasitansı. Sıra yanlışsa layout kurtaramaz
   ([[../sematik-erc/SKILL|sematik-erc]] §5).

## 8. Ön-uyum ölçümü: mutlak sayı değil, **fark** ölç

Yakın alan probu + spektrum analizörü ile alınan dBµV **laboratuvar limitiyle
karşılaştırılamaz**. Anlamlı olan tek şey **aynı kurulumda önce/sonra farkı**:
baseline al → tek bir değişiklik yap → tekrar ölç. Kurulumu (prob, mesafe, açı,
RBW, kablo pozisyonu) yaz; yazılmamış ölçüm tekrarlanamaz, dolayısıyla kanıt değil.

## 9. Bilinen sınırlar — bu skill neyi KANITLAYAMAZ

| Konu | Seviye | Neden |
|---|---|---|
| Radyasyonlu/iletilen emisyon uyumu | D | Akredite lab (L3) |
| EMC ön-uyum (yakın alan) | D | Prob + spektrum analizörü; baseline + tek değişken |
| ESD IEC 61000-4-2 dayanımı | D | ESD tabancası; ürün standardı + `esd_test_profile` olmadan PASS yok |
| ESD clamp davranışı | C→D | TLP/dinamik direnç ölçümü |
| CM choke / ferrit seçimi | C | Karma-mod S-parametre + bias verisi |
| `ZPDN(f)` anti-rezonans | C→D | Kart-özel model + ölçüm korelasyonu |
| 20-H yakın alan kuplajı | C | Ayrı değerlendirme; makale kapsamı dışı |

Ön-uyum sonucu **resmî sertifikasyon diye etiketlenmez**.

## 10. Doğrulama

```bash
# dönüş via'sı + hızlı netclass kontrolleri   (--json YAZAR)
python3 tools/scripts/board/dfm_emc_check.py board.kicad_pcb --json checks.json

# kenar / via dönüş dikişi — frekans f_knee_edge'den türetilir
python3 tools/scripts/board/stitch_density.py board.kicad_pcb --f-knee-ghz 0.7 --json stitch.json

# validator (fault-injection dahil)
python3 -m unittest -v tools.tests.test_board_checks

python3 tools/scripts/gate_receipt.py gate_receipt.json
```

> `stitch_density.py` uyarısı: `NO_COVERAGE` **çıkış koduna yansımaz**.
> Çağıran `summary.NO_COVERAGE` alanına bakmalı, yalnız exit code'a değil.
> Ayrıca `--f-knee-ghz` bayrağına verilen değerin `f_knee_edge` olduğu
> `metric_semantics`'e yazılır ([[../_shared/glossary.md]] §1).

Fault-injection (ölçülmüş referans): GND via'ların 7'si silinince metrikler
kötüleşti, hepsi silinince `NO_COVERAGE` (sahte PASS değil).

## 11. Kabul kriterleri

- [ ] Hedef standart + **baskı** + sınıf + ölçüm mesafesi `emc_plan.md`'de yazılı;
      bağışıklık port matrisi (§1) her dış port için doldurulmuş.
- [ ] Bağışıklık seviyesi **ürün standardından** seçildi; `4/8 kV` yalnız
      `LOCAL_POLICY` olarak etiketlendi ve D/L3 PASS'e yükseltilmedi.
- [ ] Kart-dışına çıkan her arayüz listelendi; her birinde filtre/choke yeri
      **fiziksel olarak ayrıldı** (kullanılmasa bile DNP).
- [ ] Hiçbir yüksek hızlı hat düzlem yarığı üzerinden geçmiyor (koordinatlı kanıt).
- [ ] Lokal dönüş via'sı eşiği **arayüz/paket kılavuzundan** geldi (kaynak yazılı,
      tek global sayı değil) ve `return_via_rules.json`'a netclass bazlı yazıldı.
- [ ] Kenar via çiti aralığı **`f_knee_edge`**'den türetildi, "tahmin" olarak
      etiketlendi; kaynağı olmayan eşik `NO_COVERAGE` üretti.
- [ ] Diferansiyel geçişte P/N'ye simetrik GSSG geometrisi doğrulandı.
- [ ] 20-H emisyon gerekçesiyle **uygulanmadı**; düzlem kenarı via ile dikildi.
- [ ] CM choke ve ferrit boncuk seçimi bias/S-parametre verisiyle gerekçelendirildi;
      boncuk+MLCC peaking'i simüle edildi.
- [ ] Decoupling planı: değer çeşitliliği değil **döngü endüktansı** üzerinden
      gerekçelendirilmiş; anti-rezonans riski yazılmış.
- [ ] Her dış arayüzde `esd_chain.json` tam; sıra **konnektör → TVS → GND →
      seri → IC**; `V_clamp@I_PP` < IC abs max **sayıyla** gösterilmiş;
      TVS GND via sayısı ≥2; TVS flow-through (stub'sız).
- [ ] TVS kapasitansı arayüzün kendi kanal bütçesinden türetildi
      (evrensel `0.5 pF` limiti kullanılmadı).
- [ ] Ön-uyum ölçümleri baseline + tek-değişken farkı olarak raporlandı;
      kurulum (prob, mesafe, açı, RBW, kablo pozisyonu) yazıldı.
- [ ] Ön-uyum sonucu resmî sertifikasyon diye etiketlenmedi.
- [ ] coverage: `scanned + excluded == eligible`, `scanned > 0`.

## İlgili

[[../konnektor/SKILL|konnektor]] · [[../empedans-stackup/SKILL|empedans-stackup]] ·
[[../highspeed-routing/SKILL|highspeed-routing]] ·
[[../crosstalk-thinning/SKILL|crosstalk-thinning]] · [[../dfm/SKILL|dfm]] ·
[[../sematik-erc/SKILL|sematik-erc]] · [[../guc-isil/SKILL|guc-isil]] ·
[[../dogrulama-matrisi/SKILL|dogrulama-matrisi]]

## Araç kancaları (2026-08-09)

> Bu araçlar registry'de kayıtlı ve testliydi ama **hiçbir skill onları
> anmıyordu**, yani bir ajan ancak elle seçerek çağırabiliyordu. Aşağıdaki
> her satır *ne zaman* çağrılacağını söyler — ad listesine eklemek tek
> başına tetiklenebilirlik sağlamaz.

- **`emc-rules`** — 3W, 20H ve λ/20 via-dikiş aralığı kural motoru; topoloji kararından hemen sonra.
- **`decoupling-coverage`** — IC güç pinlerini **board'dan keşfeder** ve her biri için en yakın uygun kapasitörün mesafesini ölçer — unutulan pin sessizce kapsam dışı kalmaz.
- **`reference-plane`** — İz segmentinin referans düzlem sürekliliği; split/kesik düzlem dönüş yolunu kırar.
- **`topology-advise`** — Ölçülmüş bir eksikliğe topoloji değişikliği önerir (ek dekuplaj kademesi, LC son-filtre, dikiş via sırası). **Yön söyler, büyüklük asla.**
