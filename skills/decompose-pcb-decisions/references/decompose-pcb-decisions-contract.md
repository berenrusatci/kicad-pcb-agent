# PCB context contract v1

## Decision unit

```json
{
  "decision_id": "ARCH-CSI-BRIDGE",
  "question": "Altı kamera tek CSI-2 girişine hangi köprüleme mimarisiyle bağlanacak?",
  "owner_skill": "pcb-orchestrator",
  "status": "ready",
  "depends_on": ["REQ-CAMERA-BW", "REQ-SCENE-MOTION"],
  "constraints": ["C-CSI-PORT-COUNT", "C-END-TO-END-LATENCY"],
  "options": ["fpga-aggregation", "multi-soc", "sensor-mux"],
  "required_evidence": ["datasheet", "bandwidth-calculation", "driver-support"],
  "output_artifact": "architecture/csi_bridge_decision.json",
  "invalidation_triggers": ["sensor-mode-change", "soc-change", "driver-missing"]
}
```

## Context manifest

Zorunlu üst alanlar:

- `schema_version`: `pcb-context-pack/v1`
- `task`: `id`, `objective`, `scope`, `non_goals`, `decision_ids`,
  `owner_skill`, `mutation_policy`, `allowed_actions`, `forbidden_actions`
- `budget`: `max_files`, `max_bytes`
- `artifacts`: `role`, `path`, `sha256`, `required`, `purpose`, `decision_ids`
- `constraints`: `id`, `statement`, `source`
- `open_questions`: `id`, `question`, `blocking`, `owner`
- `tools`: `name`, `purpose`, `mode`, `preconditions`; ad hash'li
  `pcb-tool-registry/v1` artifact'inde bulunmalıdır
- `output_contract`: `artifact_path`, `schema_version`, `required_claim_fields`
- `gates`: `id`, `role`, hash-bağlı `validator{path,sha256}`, exact `argv`,
  `expected_exit`, hash-bağlı report path+SHA ve beklenen
  `effective_status`

`artifacts` içinde tekil ve `required:true` `design_intent`, `decision_unit` ve
`tool_registry` rolleri bulunmalıdır.
Her artifact neden gerekli olduğunu `purpose` ile ve yalnız aktif task karar
kimliklerini `decision_ids` ile taşır; ilgisiz karar dosyası pakete eklenemez.
Yollar proje köküne göreli olmalı; mutlak yol veya `..` yasaktır.

`design_intent` dosyası `pcb-design-intent/v1` ve en az `goals`, `constraints`,
`non_goals`, `open_questions` taşır. `decision_unit` dosyası
`pcb-decision-units/v1` ve `decisions[]` taşır. Manifestteki karar kimlikleri
dosyada bulunmalı; `blocked` karar task açamaz. Intent içindeki blocking soru
manifestten atılamaz. Output ve gate validator yolları da proje kökü dışına çıkamaz.
`accepted` karar `evidence_ids` ve `invalidation_triggers` olmadan geçmez;
kanıt kimlikleri top-level `evidence[]` kataloğunda `canonical` olmalıdır.
Katalog girdisi `kind` ve türe bağlı provenance taşır: artifact/measurement için
root-bound `path+sha256`; official source için `document_id`, `revision`,
`publication_date`, URL `source`, `locator`, `accessed`, exact `claim_scope`,
root-bound `snapshot_path` ve `snapshot_sha256`; human sign-off için `signed_by`, `signed_at`,
`scope`. Serbest metin kaynak canonical kanıt değildir.
`depends_on` referansları eksiksiz ve çevrimsiz; karar constraint kimlikleri
manifestte ve frozen intentte mevcut olmalıdır.

`READY` yalnız preflight sonucudur. Worker sonrası `--postflight`, output
artifact'ini parse eder; accepted/rejected claim için katalogda bulunan kanıtı
zorunlu kılar ve gate exact argv'lerini shell kullanmadan çalıştırır. Gate
validator yolu argv içinde yalnız bir kez bulunmalı ve doğrudan executable ya
da `python[3] <validator>` entrypoint'i olmalıdır; yalnız argüman olarak taşımak
kapıyı açmaz. `official_source` snapshot SHA-256'sı dosyaya karşı yeniden
hesaplanır. Mandatory gate'te exit code tek başına yeterli değildir: rapor
path+SHA bağlı olmalı ve `effective_status == GREEN` olmalıdır. Nonzero
`expected_exit` yalnız `role: negative_fixture` için geçerlidir. Yalnız
`ACCEPTED` downstream tüketilebilir.

## Claim status

- `accepted`: kanıt ve doğrulama kapısı var.
- `rejected`: karşı kanıt var.
- `unverified`: kanıt eksik veya erişilemiyor.
- `conflicted`: güvenilir kaynaklar çelişiyor.
- `stale`: kaynak/input hash'i değişti.

Confidence, evidence yerine geçmez. Yüksek confidence + boş evidence yine
`unverified`dır.

## Paketleme kuralı

Paketi bir bilgi deposu olarak değil, dosya indeksli çalışma sözleşmesi olarak
tut. Worker yalnız manifestteki artifact'leri açar. Ek bağlam gerekirse çıktıda
`context_request` üretir; kendi başına tüm repo/vault'u yüklemez.
