# Yürütme zarfı — uzun koşuların ve fan-out'un ölçülen sınırları

Bu dosya [[../SKILL|paralel-yurutme]] §2d'nin sayılarını taşır. Skill ana
bağlamda yalnız kararı tutar; zarfın tamamını yüklemez.

> **Bu dosyanın tamamı `CONFIRM` seviyesindedir, `AUTHORITATIVE_SOURCE`
> değildir.** Gerekçe §4'te. Kapı bu sayılara dayanarak `GREEN` üretmez;
> yalnızca *zarfın dışına çıkma* kararını verir.

---

## 1. Foreground / arka plan zarfı

| Büyüklük | Değer | Sonucu |
|---|---|---|
| Bash varsayılan zaman aşımı | 2 dakika (`BASH_DEFAULT_TIMEOUT_MS`) | 2 dakikayı aşacak koşu foreground'da planlanmaz |
| Bash üst sınırı | 10 dakika (`BASH_MAX_TIMEOUT_MS`) | 10 dakikayı aşan validator **bloklayan** çağrıyla koşturulamaz — bu sert bir sınırdır |
| Zaman aşımına ulaşan komut | durdurulmaz, arka plana alınır | Komut hâlâ koşuyordur; "zaman aşımı" ≠ "iptal" |
| `sleep` ile başlayan komut | asla otomatik arka plana alınmaz | Bloklayan `sleep` döngüsüyle yoklama yanlış deyimdir |
| Geçerli çıktı satır-içi tavanı | ~30.000 karakter, sonrası dosya yolu + önizleme | Büyük ama başarılı çıktı **yol** verir |
| **Başarısız** çıktı satır-içi tavanı | ~10.000 karakter, baş-son kesiti, **dosya yolu YOK** | Bir dal başarısız olduğunda ham çıktısı kurtarılamaz |
| `BASH_MAX_OUTPUT_LENGTH` | varsayılan 30.000, üst sınır 150.000 | Yükseltmek satır-içi tavanı yükseltmez |

**Buradan çıkan tek kural:** her dal **kendi rapor dosyasını yazar** ve verdict
o dosyadan okunur. Repo sözleşmesi bunu zaten istiyor (`--json` daima yazar);
zarf bunun neden bir üslup tercihi değil, kurtarma şartı olduğunu söylüyor.

### exit 1 tuzağı — bu repoda somut bir örneği var

Harness, exit 1'i yalnız belirli komutlar için "geçerli sonuç" sayar (`grep`,
`rg`, `find`, `diff`, `test`, `git diff`, `git grep`). `pgrep` ve `jq -e` bu
listede **yoktur**.

Ölçüm (2026-08-11, bu makine):

```
$ pgrep -a 'zzz-no-such-process-zzz' ; echo $?
1
$ grep -c . /dev/null ; echo $?
1
```

`board-write-lock` §1 kilit kanıtını `pgrep -a 'kicad|pcbnew'` ile toplar.
**Eşleşme bulunmaması iyi haberdir** (KiCad kapalı, yazım güvenli) ama exit 1
verir — yani istenen sonuç bir komut başarısızlığı gibi görünür. Kilit kanıtını
çıkış koduna değil, **çıktının kendisine** bakarak değerlendir.

Aynı ölçümde bu depoda KiCad **açıktı** (`173733 /usr/bin/kicad`) ve
`cam-w-og05b10-wired/~cam-w-og05b10-wired.kicad_pro.lck` diskte duruyordu —
board-write-lock'un dört kanıtından ikisi pozitif. Yani bu tuzak teorik değil:
kapının doğru cevabı verdiği an, kabuk "hata" diyor.

---

## 2. Eşzamanlılık ve maliyet

| Büyüklük | Değer |
|---|---|
| Eşzamanlı alt ajan sınırı | 20 (yapılandırılabilir; aşıldığında "limit reached" döner ve yeniden denenmez) |
| İç içe alt ajan derinliği | varsayılan 3 katman |
| Dinamik workflow eşzamanlılığı | en çok 16 (çekirdek sayısına göre daha az), koşu başına toplam 1000 ajan |
| Önerilen başlangıç genişliği | 3–5 dal |
| Yayımlanmış token oranı | ajan koşusu ≈ **4×** sohbet · çok-ajanlı sistem ≈ **15×** sohbet |

**4× ve 15× aynı tabana (sohbete) göredir.** İkisini birbirine bölerek
"çok-ajanlı tek-ajanlıya göre 3.75×" gibi bir sayı türetmek kaynağın söylemediği
bir iddiadır; skill bunu yasaklar.

Bağlam tarafında iki sonuç:

- Alt ajan **taze bağlamla** başlar: ana sohbeti, okunmuş dosyaları ve çağrılmış
  skill'leri görmez. Bu fan-out'un hem kazancı (izolasyon) hem bedelidir
  (bağlamı yeniden toplama).
- `CLAUDE.md` hiyerarşisi her alt ajana yüklenir → fan-out `CLAUDE.md`
  maliyetini dal sayısıyla çarpar. Bu depoda kök `CLAUDE.md` **yok**
  (2026-08-11 ölçümü), yani şu an bu çarpan sıfırdır; bir tane eklenirse
  paralel dal maliyetini de artıracağı burada kayıtlıdır.

---

## 3. Aranan ve **bulunamayan** sayılar

Kapının eşiksiz olmasının gerekçesi budur (§2c'deki dört soru bir eşiğin yerini
tutar, çünkü eşik yok):

| İddia | Nerede geçiyor | Durum |
|---|---|---|
| "Ajan başlatma maliyeti ~2–5 saniye" | arXiv özet parçası | **Doğrulanamadı** — kaynak metinde varlığı teyit edilemedi; kullanılmaz |
| "15 dakikadan uzun alt görevler için fan-out kârlı" | aynı parça | **Doğrulanamadı** — bulunan tek sayısal eşik iddiası ve arkasında ölçüm yok |
| "Task tool en çok 10 eşzamanlı" | birden çok blog | **Yanlış** — resmi belge 20 diyor; eski ya da uydurma |
| "Dört adet 30 saniyelik işi paralelleştirmek kazandırdığından fazlasına mal olur" | Anthropic'e atfediliyor | **Yanlış atıf** — atfedilen yazıda böyle bir cümle yok |
| Ölçülmüş minimum iş boyutu (saniye/token/dosya) | — | **Yok.** Ne üreticide ne pratikte yayımlanmış ölçülmüş bir eşik bulundu |

Resmî kaynakların bu konuda söylediği **nitel**dir ve skill §2c onu birebir
kullanır: çok küçük iş için koordinasyon maliyeti kazancı aşar; hızlı ve odaklı
bir düzeltmede devretmenin maliyeti faydasından fazladır; gecikme kritikse alt
ajan taze başladığı için bağlam toplaması zaman alır.

---

## 4. Neden `AUTHORITATIVE_SOURCE` değil

[[../../_shared/coverage-gate.md]] §`evidence_origin` sözlüğü,
`AUTHORITATIVE_SOURCE` için `document_id` + `revision` + `locator` +
`accessed` + `snapshot_sha256` **zorunlu** kılar. Yukarıdaki satırların
`snapshot_sha256`'ı yok ve kaynak belgeler sürümle değişiyor — belgenin kendisi
eşzamanlılık ve derinlik sınırlarının sürümler arasında değiştiğini yazıyor
(derinlik: 5 → 1 → 3). Snapshot'suz bir sayı, yarın sessizce yanlış olabilen bir
sayıdır.

Bu yüzden:

- Bu dosyanın satırları `CONFIRM`'dür; hiçbir kapı bunlara dayanarak `GREEN`
  üretmez.
- Bir sayı karar değiştirecekse (ör. "10 dakikayı aşıyor mu") **o an ölçülür**,
  tabloya güvenilmez: koşuyu başlat, süresini yaz.
- Tabloyu tazeleyen kişi `accessed` tarihini ve varsa `snapshot_sha256`'ı
  ekler; eklemeden güncellemek drift'i gizler.

**Locator'lar** (hepsi `accessed: 2026-08-11`, `snapshot_sha256: yok`):

- `code.claude.com/docs/en/tools-reference` — Bash zaman aşımı, çıktı
  tavanları, exit 1 sayılan komut listesi, arka plan davranışı
- `code.claude.com/docs/en/sub-agents` — eşzamanlılık sınırı, iç içe derinlik,
  bağlam izolasyonu, `CLAUDE.md` yüklenmesi, inline kalma ölçütleri
- `code.claude.com/docs/en/workflows` — dinamik workflow eşzamanlılığı ve
  toplam ajan sınırı
- `code.claude.com/docs/en/agent-teams` — iş boyutlandırma ("çok küçük:
  koordinasyon maliyeti kazancı aşar"), dosya çakışmasından kaçınma, 3–5
  başlangıç genişliği
- `anthropic.com/engineering/multi-agent-research-system` — 4× / 15× token
  oranları, orkestratör-işçi kalıbı, 3–5 alt ajan genişliği, "aynı aramayı iki
  ajanın yapması" başarısızlığı
- `anthropic.com/engineering/effective-context-engineering-for-ai-agents` —
  alt ajanın on binlerce token harcayıp 1.000–2.000 token özet döndürmesi
- `man7.org/linux/man-pages/man1/flock.1.html` — `-x` / `-n` / `-w` ve fd
  tabanlı alt kabuk deyimi (bu skill kilit **kullanmaz**; düzenle çözer, ama
  kilit gerekirse kanonik deyim burada)

---

## İlgili

[[../SKILL|paralel-yurutme]] ·
[[../../pcb-orchestrator/references/board-write-lock.md]] ·
[[../../pcb-orchestrator/references/phase-dag.md]] ·
[[../../_shared/coverage-gate.md]]
