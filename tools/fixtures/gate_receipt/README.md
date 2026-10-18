---
tags: [fixture, gate-receipt, test]
durum: staging
---

# Gate receipt fixtures

Bu dizin yalnız staging test verisidir. Canlı vault'a yazmaz ve test sırasında
canlı dosya yolu kullanmaz.

- `usb_pair_positive.json`: gerçek USB-HS `review-2l` receipt'inin legacy
  `schema: usb-hs-pair-verifier-receipt/v1` şeklini koruyup kanonik kanıt
  alanlarıyla tamamlanmış pozitif fixture. Gerçek p50/in-band örnekleri ve
  `/USB_D_N` için 0,30 mm fault'un reddedildiği kontrol isimleri korunur.
- `usb_pair_metrics_minimal.json`: gerçek 24 numaralı metrics JSON'un yalnız
  hassas-olmayan, test için gereken alt kümesi.
- `usb_pair_verifier_fixture.py`: kaynak verifier'ın yürütülmeyen kimlik stub'ı;
  `pcbnew` gerektirmez.
- `review_2l_rules.json`: pozitif fixture'ın hash-bağlı profil kuralları.
- `usb_pair_gap_fault_definition.json`: gerçek 0,30 mm gap enjeksiyonunun
  hash-bağlı minimal fixture tanımı.
- `usb_pair_gap_fault_observation.json`: source receipt'teki reddedilen metric
  ID'lerini baseline hash'ine bağlayan minimal injected observation.
- `provenance.json`: canlı kaynağın göreli 22–25 yolları, source SHA-256'ları,
  normalizasyon notu ve `synthetic: false` kaydı.

Negatif fixture'lar diskte kalıcı kopya değildir. Test harness pozitif dizini
geçici klasöre kopyalayıp stale/hash/semantics/coverage/child/DRC/promotion
fault'larını orada deterministik üretir.
Semantic-ref testleri hash-bağlı `{metric_id,path,sha256}` JSON'larını, RED
promotion testleri de ayrı bir geçici canonical dosyanın before/after/current
hash zincirini aynı geçici dizinde üretir.
