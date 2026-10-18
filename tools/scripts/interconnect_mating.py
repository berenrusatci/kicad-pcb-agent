#!/usr/bin/env python3
"""A'nın fiziksel çıkışı B'nin fiziksel girişine takılır mı, ve pinler örtüşür mü?

BU SORUNUN REPODA SAHİBİ YOKTU
------------------------------
Netlist ELEKTRİKSELDİR: iki pin aynı nete bağlıysa bağlıdır, o pinlerin
fişleri birbirine giriyor mu diye sormaz. Footprint TEK PARÇADIR: bir pad
dizisinin kendi içinde tutarlı olduğunu söyler, karşısına ne takıldığını
bilmez. Keepout HACİMDİR: iki gövdenin çakışmadığını söyler, çakışmamak
takılmak değildir. Hiçbiri EŞLEŞME değildir.

EVREN, BEYAN LİSTESİNDEN TÜREMEZ - KAPININ HAYAT MEMAT MESELESİ
---------------------------------------------------------------
``eligible`` evreni ``interface_contract.json``'ın GEREKTİRDİĞİ bağlantı
kümesinden türer - "olması gerekenler"den, "birinin yazmayı hatırladıkları"ndan
DEĞİL. Sebep ölçülmüştür ve bu deponun kendi belgesinde yazılıdır
(``docs/BULGU-bos-kontrol-temiz-gorunur.md:79-85``): iki halka beyan edilip
üçüncüsü unutulduğunda ``eligible=2, scanned=2, violations=0 -> GREEN`` olur ve
eksik halka evrene HİÇ GİRMEZ. Teardrop vakasının birebir aynısı: *"Yanlış
populasyondan gelen bir payda, gerçek bir muhafızla aynı yeşili verir."*

    eligible   = interface_contract'taki zorunlu bağlantıdan türetilen kenar
                 evreni:  Sigma_düğüm (yüz_sayısı - 1)  +  dış_kenar_sayısı
    scanned    = eligible - excluded  (kaydı OLMAYAN kenar da taranır ve
                 ihlal sayılır; evrenden DÜŞMEZ)
    excluded   = AÇIK gerekçeli --exclude-link <id>:<gerekçe>
    violations = ihlal veren scanned kenar sayısı,  violations <= scanned

Zorunlu bir arayüzün zinciri hiç yoksa o arayüz ``eligible``'da EN AZ BİR
kenarla durur ve ``MISSING_TRANSITION`` ile ihlal sayılır. Beyanı silmek
paydayı küçültmez; paydanın kaynağı başka dosyadır.

SAYI EŞİTLİĞİ KİMLİK EŞİTLİĞİ DEĞİLDİR - R4 İMZA AÇMAZ
------------------------------------------------------
20 pozisyonlu breakout + 20 pozisyonlu FPC "20=20" geçer, ama pin 1 karşı uçta
pin 20'ye düşüyorsa zincir yanlıştır. Bu yüzden R4 ``authority: screen_only``:
temiz bir R4 imza açmaz, imzayı R10 (``pin_to_pin[]`` zorunlu) + R11 (net
kimliği) taşır. Kanonik kanıt: Raspberry Pi 5'te CSI ve DSI portları FİZİKSEL
OLARAK ÖZDEŞ - 22-pin, 0.5 mm, 11.5 mm - ama kablolar birbirinin yerine geçmez
ve RPi bunları ayrı SKU olarak satar.

Bir ``screen_only`` kuralın FAIL'i yine de kapıyı kapatır: 30 kontaklı bir
kablo 22 kontaklı bir yuvaya girmez. ``screen_only`` "PASS imzalamaz" demektir,
"FAIL bir şey söylemez" demek değildir. ``record_only`` bir FAIL ise kapıyı
kapatmaz, bir ``CONFIRM`` cap ekler.

R12 ÖLDÜRÜCÜDÜR
---------------
Yanlış pin haritası MIPI'de "çalışmaz" değildir: 3V3 rayının bir D-PHY lane'ine
binmesi sensör veya ISP girişinde KALICI HASARDIR. R6 sessiz açık devre üretir,
R12 sessiz hasar. İkisi aynı önceliğe konmaz.

ALIAS TABLOSU YASAKTIR
----------------------
Pin haritası hizalama pin NUMARASI üzerinden değil, beyan edilmiş fiziksel
kontak konumu üzerinden yapılır; ad karşılaştırması BİREBİRDİR.
``MIPI_D0_P`` != ``CSI_D0_P`` bir İHLALDİR ve çözümü normalleştirme değil,
sözleşmenin veya kartın düzeltilmesidir - ``ecad-varlik-kalifikasyonu:130-133``:
*"Normalleştirici, yanlış pinin sessizce doğru pine dönüştüğü yerdir."*

SATICI HARFİ ASLA ANAHTAR OLMAZ
-------------------------------
Kablo kontak-yüzü tipini adlandıran evrensel standart yoktur: Molex ``A``/``D``,
Würth ``1``/``2``, piyasa jargonu ``A``/``B`` - ve MOLEX'TE "B" DİYE BİR TİP
YOKTUR. Satıcı harfi anahtar yapılırsa Molex parçasında sessizce yanlış çalışır.
Bu araç yalnız ``SAME_SIDE``/``OPPOSITE_SIDE`` normalize enum'unu okur; satıcı
harfi ``provenance`` içinde ham veri olarak yaşar ve hiçbir kararı beslemez.

BU ARAÇ NE YAPMAZ
-----------------
Parça SEÇMEZ (``bom-lifecycle``), net BAĞLAMAZ / şematik AÇMAZ
(``sematik-erc``), hacim çakışması ÖLÇMEZ (``mekanik-dxf``),
akım/derating/mating-cycle bütçesi HESAPLAMAZ (``konnektor``), tek parçanın
sembol/footprint kimliğini ÖLÇMEZ (``ecad-varlik-kalifikasyonu`` - bu araç
onun KARDEŞİDİR, üstü değil). ``writes_board: false``, ``signoff: false``,
``assurance_loop: [L1, L2]`` - ``L3`` YOK, çünkü hiçbir fiziksel ölçüm
yayımlamaz.

Kullanım:
    interconnect_mating.py --contract interface_contract.json \\
                           --chain interconnect_chain.json [--json report.json]
    interconnect_mating.py --self-test

Çıkış kodları: 0 = GREEN, 1 = RED / NO_COVERAGE / CONFIRM / ..., 2 = araç
cevap üretemedi (bozuk girdi, bozuk kapsam aritmetiği, yazma hatası).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (FAIL, Finding, NO_COVERAGE, PASS,  # noqa: E402
                             by_check_map, coverage_rule_violations, result,
                             rollup_status, summarise)
from common.hashing import sha256_file  # noqa: E402
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)

SCHEMA_VERSION = "interconnect-mating-report/v1"
TOOL_NAME = "interconnect_mating"
CHAIN_SCHEMA_VERSION = "interconnect-chain/v1"

# The closed verdict vocabulary (skills/_shared/coverage-gate.md:84-88). Nothing
# below is invented here; every name already exists there.
GREEN = "GREEN"
RED = "RED"
CONFIRM = "CONFIRM"
SCREEN_ONLY = "SCREEN_ONLY"
LOCAL_POLICY = "LOCAL_POLICY"
NO_COV = "NO_COVERAGE"
INVALID_COVERAGE = "INVALID_COVERAGE"
STALE = "STALE"

# A cap never opens a gate; it says HOW FAR from GREEN the run is and why.
CAP_ORDER: Tuple[str, ...] = (CONFIRM, LOCAL_POLICY, SCREEN_ONLY)

# DOMAIN STATE, not a verdict. The closed vocabulary is for gate.verdict only;
# "chain complete / link missing / adapter unspecified" is a domain answer and
# lives in its own field with its own vocabulary (coverage-gate.md:90-112).
CHAIN_COMPLETE = "CHAIN_COMPLETE"
CHAIN_BROKEN = "CHAIN_BROKEN"
MEDIUM_UNSPECIFIED = "MEDIUM_UNSPECIFIED"
PINMAP_UNVERIFIED = "PINMAP_UNVERIFIED"
DECLARED_NONMATCHING = "DECLARED_NONMATCHING"
# Most specific first: a side that declared itself wrong outranks a hole.
DOMAIN_ORDER = (DECLARED_NONMATCHING, CHAIN_BROKEN, MEDIUM_UNSPECIFIED,
                PINMAP_UNVERIFIED, CHAIN_COMPLETE)

# Authority axis (dogrulama-matrisi §1 "Seviye tek eksen değildir").
SIGNOFF = "signoff"
SCREEN = "screen_only"
RECORD = "record_only"

# Provenance. All five, or the record is not an authoritative source - it is a
# note (coverage-gate.md, AUTHORITATIVE_SOURCE row).
PROVENANCE_FIELDS: Tuple[str, ...] = (
    "document_id", "revision", "locator", "accessed", "snapshot_sha256")
BASIS_SIGNOFF = ("datasheet", "measured")
BASIS_LOCAL = ("local_policy",)
BASIS_SCREEN = ("derived", "vendor_page")

# Reason codes. Every one is a case a permissive version would have called PASS.
NO_MATED_PART = "NO_MATED_PART"
MISSING_TRANSITION = "MISSING_TRANSITION"
INTERFACE_CLASS_MISMATCH = "INTERFACE_CLASS_MISMATCH"
PITCH_MISMATCH = "PITCH_MISMATCH"
POSITION_COUNT_MISMATCH = "POSITION_COUNT_MISMATCH"
GENDER_CONFLICT = "GENDER_CONFLICT"
CONTACT_FACE_MISMATCH = "CONTACT_FACE_MISMATCH"
CABLE_TYPE_MISMATCH = "CABLE_TYPE_MISMATCH"
TAIL_THICKNESS_OUT_OF_RANGE = "TAIL_THICKNESS_OUT_OF_RANGE"
TAIL_GEOMETRY_MISMATCH = "TAIL_GEOMETRY_MISMATCH"
PINMAP_UNDECLARED = "PINMAP_UNDECLARED"
MAPPING_UNDECLARED = "MAPPING_UNDECLARED"
NET_NAME_MISMATCH = "NET_NAME_MISMATCH"
PIN_ORDER_MISMATCH = "PIN_ORDER_MISMATCH"
POWER_ON_SIGNAL_PIN = "POWER_ON_SIGNAL_PIN"
DIFFPAIR_SWAP = "DIFFPAIR_SWAP"
DIFFPAIR_SPLIT = "DIFFPAIR_SPLIT"
LANE_COUNT_MISMATCH = "LANE_COUNT_MISMATCH"
STACK_HEIGHT_MISMATCH = "STACK_HEIGHT_MISMATCH"
NOT_A_VALID_MATING_PART = "NOT_A_VALID_MATING_PART"
UNVERIFIED_ATTRIBUTE = "UNVERIFIED_ATTRIBUTE"
BLOCKED_UNSPECIFIED = "BLOCKED_UNSPECIFIED"
PLATING_MISMATCH = "PLATING_MISMATCH"
MATING_CYCLE_BUDGET = "MATING_CYCLE_BUDGET"
DECLARED_NONMATCHING_CODE = "DECLARED_NONMATCHING"
UNJUSTIFIED_EXCLUSION = "UNJUSTIFIED_EXCLUSION"
UNKNOWN_EXCLUSION = "UNKNOWN_EXCLUSION"
CONTRACT_REF_UNRESOLVED = "CONTRACT_REF_UNRESOLVED"
ELIGIBLE_FROM_CHAIN_REFUSED = "ELIGIBLE_FROM_CHAIN_REFUSED"
NODE_DECLARATION_CONFLICT = "NODE_DECLARATION_CONFLICT"
ENDPOINT_UNRESOLVED = "ENDPOINT_UNRESOLVED"
BOM_LINE_ABSENT = "BOM_LINE_ABSENT"

RAIL_CLASSES = frozenset({"POWER", "GND"})
SIGNAL_CLASSES = frozenset({"SIGNAL", "DIFF_P", "DIFF_N", "CLOCK_P", "CLOCK_N"})
DIFF_CLASSES = frozenset({"DIFF_P", "DIFF_N", "CLOCK_P", "CLOCK_N"})
POSITIVE_CLASSES = frozenset({"DIFF_P", "CLOCK_P"})

DIRECT_MATE = "DIRECT_MATE"

# Which connector interface classes a medium of each kind can bridge. A pairing
# not in this table is not "probably fine": it is INTERFACE_CLASS_MISMATCH.
MEDIUM_CLASS_COMPAT: Dict[str, frozenset] = {
    "FFC_CABLE": frozenset({"FFC_ZIF", "FPC_ZIF"}),
    "FPC": frozenset({"FFC_ZIF", "FPC_ZIF"}),
    "DISCRETE_HARNESS": frozenset({"WIRE_TO_BOARD", "HEADER", "TERMINAL_BLOCK"}),
    "COAX_ASSEMBLY": frozenset({"WIRE_TO_BOARD", "RF_COAX"}),
    "ADAPTER_BOARD": frozenset({"BOARD_TO_BOARD", "MEZZANINE", "CARD_EDGE",
                                "HEADER", "WIRE_TO_BOARD", "FFC_ZIF",
                                "FPC_ZIF"}),
}
GENDER_OK = frozenset({("RECEPTACLE", "PLUG"), ("PLUG", "RECEPTACLE"),
                       ("HERMAPHRODITIC", "HERMAPHRODITIC")})
OPPOSITE_SIDE_OF = {"TOP": "BOTTOM", "BOTTOM": "TOP"}

# Keys a chain may NOT carry. The denominator does not come from this file, and
# a file that tries to state it is refused rather than ignored: ignoring it
# silently would leave a reader believing the number was honoured.
FORBIDDEN_CHAIN_KEYS = ("eligible", "coverage", "coverage_hint",
                        "edge_count", "declared_eligible")

LIMITS = [
    "İKİ BELGEYİ karşılaştırır; hiçbir fiziksel eşleşme ölçmez. Gerçek mating "
    "kuvveti, hizalama toleransı ve aktüatör kilidi D/L3'tür ve "
    "l3-bringup-karakterizasyon'a devredilir - bu araç limitin sahibidir, "
    "ölçümün değil",
    "Pin-1 köşelerinin uzaysal ilişkisi (mirroring paritesi) HİÇBİR datasheet "
    "alanında taşınmaz; mekanik yerleşimden türer. pin1_reference / "
    "numbering_direction / contact_face eksikse ilgili kural NO_COVERAGE'dır "
    "ve pin_to_pin[] çizelgesini bir insan imzalar",
    "'Mates with' ilişkisinin DOĞRULUĞUNU kanıtlayamaz: üreticiler bunu "
    "makine-okunur yayımlamıyor ve ölçülmüş bir vakada Molex'in kendi "
    "datasheet'i kendi ordering table'ıyla çelişiyor. valid_mating_mpns bir "
    "İNSAN GİRİŞİDİR ve R16 record_only'dir",
    "NDA'lı pinout (OG05B10 89-pin CSP) kapalıdır: BLOCKED_UNSPECIFIED yazar, "
    "'geçti' DİYEMEZ",
    "Datasheet'siz COTS modülün pin sırası C->D'dir; satıcı web sayfası "
    "otorite değildir (basis=vendor_page -> en fazla SCREEN_ONLY)",
    "Bir zincirin ARADAKİ bir düğümü hiç yazmadığını göremez: düğüm sayısı bir "
    "BEYANDIR. Kanıtlayabildiği şey, sözleşmenin gerektirdiği her arayüzün bir "
    "zinciri olduğu ve beyan edilen hiçbir halkanın medium'suz olmadığıdır",
    "Kablonun gerçek bükülme/yönlendirme yolu ve uzunluğu mekanik-dxf + L3'ün "
    "alanıdır (IPC-D-620A §8.2 clocking)",
    "Kontak akım paylaşımı, launch discontinuity ve derating konnektor + L3'ün "
    "alanıdır; bu araç hiçbir akım sayısı okumaz",
    "Hacim çakışması ölçmez: çakışmamak takılmak değildir (mekanik-dxf)",
    "Kablo/adaptör MPN'ini SEÇMEZ ve BOM'u düzeltmez: seçim, stok ve lifecycle "
    "bom-lifecycle'ın alanıdır. BOM'da olmayan bir kablo CONFIRM'dir (RED "
    "değil) + L2 kenarı",
    "Net BAĞLAMAZ, şematik/board AÇMAZ: netin karşı uçta hangi fiziksel pine "
    "düştüğü kararını receipt'e YAZAR, sematik-erc uygular. writes_board=false "
    "ve signoff=false pinlidir",
]


class InterconnectMatingError(ValueError):
    """Koşu bir cevap üretemez. Asla bir verdict'e indirgenmez."""


# --------------------------------------------------------------------------- #
# 0. Girdi
# --------------------------------------------------------------------------- #

def read_json(path: Path, label: str) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InterconnectMatingError(
            f"{label}: {path} okunamadı: {exc}") from exc
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InterconnectMatingError(
            f"{label}: {path} geçerli JSON değil: {exc}") from exc
    if not isinstance(doc, dict):
        raise InterconnectMatingError(f"{label}: {path} bir JSON nesnesi olmalı")
    return doc


def _artifact(path: Optional[Path]) -> dict:
    if path is None:
        return {"path": "", "sha256": None}
    p = Path(path)
    return {"path": str(p), "sha256": sha256_file(p) if p.is_file() else None}


def _num(value: Any) -> Optional[float]:
    """A measured number, or None. A measured quantity is NEVER defaulted to 0:
    ``0`` compares equal to a threshold and an unmeasured value must not."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --------------------------------------------------------------------------- #
# 1. Sözleşme - PAYDANIN kaynağı
# --------------------------------------------------------------------------- #

class ContractLink:
    """interface_contract.json'ın GEREKTİRDİĞİ bir bağlantı.

    Bu nesnelerin sayısı paydanın tabanıdır. Zincir dosyası bu listeyi
    genişletemez ve daraltamaz.
    """

    def __init__(self, link_id: str, pointer: str, detail: dict):
        self.link_id = link_id
        self.pointer = pointer
        self.detail = detail

    def to_dict(self) -> dict:
        return {"link_id": self.link_id, "pointer": self.pointer,
                **self.detail}


def parse_contract(doc: dict, path: Path) -> Tuple[List[ContractLink], str]:
    """Zorunlu bağlantı kümesi + sözleşmenin biçimi.

    İKİ BİÇİM de okunur ve bu bir uyum jesti değil, ölçülmüş bir gerçektir:
    kanonik ``interface-contract/v1`` şeması ``interfaces[]`` dizisi bekler,
    ama depodaki gerçek kart dosyası (``cam-w-og05b10/docs/interface_contract.json``)
    ``$schema: pcb-agent/interface_contract/v1`` ile adlandırılmış BÖLÜMLER
    taşıyor (``host_uplink``, ``sensor_module``). Bir tanesini okuyup diğerini
    reddetmek, kapıyı deponun kendi verisine kapatmak olurdu; ikisini birden
    okuyup farkı SUSMAK ise iki sözleşme biçiminin var olduğunu gizlemek olurdu.
    Bu yüzden biçim rapora yazılır.
    """
    version = _text(doc.get("schema_version"))
    if version == "interface-contract/v1":
        interfaces = doc.get("interfaces")
        if not isinstance(interfaces, list):
            raise InterconnectMatingError(
                f"contract: {path} 'interfaces' dizisi taşımıyor")
        links: List[ContractLink] = []
        for index, entry in enumerate(interfaces):
            if not isinstance(entry, dict):
                raise InterconnectMatingError(
                    f"contract: interfaces[{index}] bir nesne olmalı")
            if _text(entry.get("decision_status")) == "rejected":
                continue
            link_id = _text(entry.get("interface_id")) or f"interfaces[{index}]"
            links.append(ContractLink(
                link_id, f"interfaces/{link_id}",
                {"connector_refdes": entry.get("connector_refdes"),
                 "role": entry.get("role"),
                 "leaves_board": entry.get("leaves_board"),
                 "cable_present": entry.get("cable_present"),
                 "decision_status": entry.get("decision_status"),
                 "index": index}))
        return links, "interface-contract/v1"

    schema_tag = _text(doc.get("$schema")) or ""
    if schema_tag.startswith("pcb-agent/interface_contract"):
        links = []
        for key, entry in doc.items():
            if not isinstance(entry, dict):
                continue
            ref = _text(entry.get("ref"))
            if ref is None:
                # ``connector_common`` and friends: shared attributes, not an
                # interface. An interface is a section that names a connector.
                continue
            links.append(ContractLink(
                key, key,
                {"connector_refdes": ref,
                 "name": entry.get("name"),
                 "role": None, "leaves_board": None,
                 "cable_present": None, "decision_status": None}))
        if not links:
            raise InterconnectMatingError(
                f"contract: {path} kart-yerel biçimde ama 'ref' taşıyan hiçbir "
                "arayüz bölümü yok - zorunlu bağlantı kümesi türetilemez. "
                "Boş bir payda GREEN üretemez; bu yüzden bu bir hatadır, "
                "sessiz bir NO_COVERAGE değil")
        return links, "board-local/pcb-agent-interface_contract-v1"

    raise InterconnectMatingError(
        f"contract: {path} tanınmayan bir sözleşme biçimi "
        f"(schema_version={version!r}, $schema={schema_tag!r}). Sürüm "
        "yoksayılmaz: sonraki bir revizyon bu karşılaştırmanın dayandığı bir "
        "alanı yeniden tanımlayabilir")


CONNECTOR_SPEC_VERSION = "connector-spec/v1"
CONNECTOR_SPEC_ABSENT = "CONNECTOR_SPEC_ABSENT"
SPEC_CONTRADICTS_CHAIN = "SPEC_CONTRADICTS_CHAIN"
REFDES_NOT_IN_CONNECTOR_SPEC = "REFDES_NOT_IN_CONNECTOR_SPEC"

# Hangi yüz alanı `connector_spec.json`'daki hangi alanla karşılaştırılır.
# İkisi de AYNI büyüklüğü iddia ediyor; iki belge tek bir büyüklük hakkında
# anlaşamıyorsa bundan sonra ikisine de inanılamaz.
SPEC_CROSS_FIELDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("pitch_mm", ("pitch_mm",)),
    ("positions", ("positions", "position_count", "contacts")),
    ("mating_cycles", ("mating", "datasheet_cycles")),
    ("contact_finish", ("contact_finish",)),
)


def parse_connector_spec(doc: dict, path: Path) -> Dict[str, dict]:
    version = _text(doc.get("schema_version"))
    if version != CONNECTOR_SPEC_VERSION:
        raise InterconnectMatingError(
            f"connector_spec: {path} schema_version {version!r} bildiriyor, bu "
            f"araç {CONNECTOR_SPEC_VERSION!r} okur. Sürüm yoksayılmaz: sonraki "
            "bir revizyon bu karşılaştırmanın dayandığı bir alanı yeniden "
            "tanımlayabilir")
    entries = doc.get("connectors")
    if not isinstance(entries, list):
        raise InterconnectMatingError(
            f"connector_spec: {path} 'connectors' dizisi taşımıyor")
    by_refdes: Dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        refdes = _text(entry.get("refdes"))
        if refdes is None:
            continue
        if refdes in by_refdes:
            raise InterconnectMatingError(
                f"connector_spec: {refdes} iki kez geçiyor; hangi kaydın "
                "karşılaştırıldığı sıraya bağlı olurdu")
        by_refdes[refdes] = entry
    return by_refdes


def _spec_value(entry: dict, keys: Sequence[str]) -> Any:
    node: Any = entry
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def spec_cross_check(edges: Sequence[Edge],
                     spec: Optional[Dict[str, dict]]) -> Tuple[Finding, List[dict]]:
    """`connector_spec.json` ile zincir kaydı AYNI büyüklüğü söylüyor mu?

    Bu girdi opsiyoneldir ama **sessiz değildir**. Yoksa koşu bir `CONFIRM`
    cap taşır ve `GREEN` üretemez (`coverage-gate.md:21` — *"eksik kanıt
    CONFIRM'dür, PASS değildir"*). Varsa **tüketilir**: beyan edilip hiç
    okunmayan bir girdi, bu araştırmanın adını koyduğu kusur sınıfının
    kendisidir (*declared-but-ungated*) — ölçülmüş emsali `contact_face`'in
    şemada `required` olup tüketicisi sıfır olmasıdır.

    Karşılaştırma yönü önemli: burada `connector_spec` bir **iddia**dır,
    kaynak değil. İki belge tek büyüklük hakkında anlaşamıyorsa bu bir `FAIL`'dir,
    çünkü ondan sonra ikisine de inanılamaz (emsal:
    `connector_current.py::SPEC_BLOCK_CONTRADICTS`).
    """
    check = "connector_spec_zincirle_uyusuyor"
    if spec is None:
        return (Finding(check, NO_COVERAGE, 0, [],
                        "connector_spec.json verilmedi: konnektör nitelikleri "
                        "yalnız zincir kaydından okundu, çapraz doğrulama "
                        "YAPILMADI"), [])
    violations: List[dict] = []
    scanned = 0
    for edge in edges:
        for side in (edge.side_a, edge.side_b):
            if side is None or side.get("kind") != "face":
                continue
            refdes = side.get("refdes")
            if refdes is None:
                continue
            entry = spec.get(refdes)
            if entry is None:
                violations.append({
                    "edge_id": edge.edge_id, "refdes": refdes,
                    "codes": [REFDES_NOT_IN_CONNECTOR_SPEC],
                    "detail": "zincir bu refdes'i taşıyor, connector_spec "
                              "taşımıyor: konnektör kaydı ile zincir kaydı "
                              "aynı kartı anlatmıyor"})
                continue
            scanned += 1
            for field, keys in SPEC_CROSS_FIELDS:
                mine = side.get(field)
                theirs = _spec_value(entry, keys)
                if mine is None or theirs is None:
                    continue          # null == null eşitlik değildir
                if isinstance(mine, float) or isinstance(theirs, float):
                    same = abs(float(mine) - float(theirs)) < 1e-9
                else:
                    same = str(mine) == str(theirs)
                if not same:
                    violations.append({
                        "edge_id": edge.edge_id, "refdes": refdes,
                        "field": field, "chain": mine, "connector_spec": theirs,
                        "codes": [SPEC_CONTRADICTS_CHAIN],
                        "detail": f"{refdes}.{field}: zincir {mine!r}, "
                                  f"connector_spec {theirs!r} diyor - iki belge "
                                  "tek büyüklük hakkında anlaşamıyorsa "
                                  "ikisine de inanılamaz"})
    if scanned == 0:
        return (Finding(check, NO_COVERAGE, 0, violations,
                        "connector_spec verildi ama zincirdeki hiçbir refdes "
                        "onda bulunamadı"), violations)
    return (result(check, scanned, violations,
                   f"{scanned} yüz connector_spec kaydıyla karşılaştırıldı"),
            violations)


def _pointer_of(ref: str) -> str:
    fragment = ref.split("#", 1)[1] if "#" in ref else ref
    return fragment.lstrip("/").strip()


def match_contract_link(ref: Optional[str],
                        links: Sequence[ContractLink]) -> Optional[ContractLink]:
    if ref is None:
        return None
    pointer = _pointer_of(ref)
    if not pointer:
        return None
    tail = pointer.rsplit("/", 1)[-1]
    for link in links:
        if pointer in (link.pointer, link.link_id) or tail == link.link_id:
            return link
    return None


# --------------------------------------------------------------------------- #
# 2. Zincir
# --------------------------------------------------------------------------- #

def parse_chain(doc: dict, path: Path) -> List[dict]:
    version = _text(doc.get("schema_version"))
    if version != CHAIN_SCHEMA_VERSION:
        raise InterconnectMatingError(
            f"chain: {path} schema_version {version!r} bildiriyor, bu araç "
            f"{CHAIN_SCHEMA_VERSION!r} okur")
    chains = doc.get("chains")
    if not isinstance(chains, list):
        raise InterconnectMatingError(f"chain: {path} 'chains' dizisi taşımıyor")
    seen: set = set()
    for index, chain in enumerate(chains):
        if not isinstance(chain, dict):
            raise InterconnectMatingError(f"chain: chains[{index}] nesne olmalı")
        for key in FORBIDDEN_CHAIN_KEYS:
            if key in chain:
                raise InterconnectMatingError(
                    f"chain: chains[{index}] {key!r} alanını taşıyor -> "
                    f"{ELIGIBLE_FROM_CHAIN_REFUSED}. Payda BU DOSYADAN "
                    "gelmez; interface_contract.json'ın gerektirdiği bağlantı "
                    "kümesinden türer. Beyan listesinden türeyen bir payda, "
                    "iki halka beyan edilip üçüncüsü unutulduğunda GREEN "
                    "verir ve eksik halka evrene hiç girmez")
        chain_id = _text(chain.get("chain_id"))
        if chain_id is None:
            raise InterconnectMatingError(
                f"chain: chains[{index}] 'chain_id' taşımıyor")
        if chain_id in seen:
            raise InterconnectMatingError(
                f"chain: {chain_id!r} iki kez bildirilmiş; hangi kaydın "
                "geçerli olduğu sıraya bağlı olurdu")
        seen.add(chain_id)
        if _text(chain.get("contract_ref")) is None:
            raise InterconnectMatingError(
                f"chain: {chain_id!r} 'contract_ref' taşımıyor. Sözleşmeye "
                "bağlanmamış bir zincir, tam olarak 'birinin yazmayı "
                "hatırladıkları' kümesidir ve payda olamaz")
        if not isinstance(chain.get("nodes"), list) or not chain["nodes"]:
            raise InterconnectMatingError(
                f"chain: {chain_id!r} boş olmayan 'nodes' dizisi taşımalı")
        if not isinstance(chain.get("interconnects"), list):
            raise InterconnectMatingError(
                f"chain: {chain_id!r} 'interconnects' dizisi taşımalı")
    return chains


# --------------------------------------------------------------------------- #
# 3. Kenar evreni
# --------------------------------------------------------------------------- #

class Edge:
    """Bir kenar: ya bir eşleşme (dış) ya bir iç geçiş.

    İKİ SINIF DA ``eligible``'a girer. ``mating`` fiziksel takılabilirliği,
    ``internal`` kart netlist sürekliliğini / kablonun düz-ters eşlemesini
    ölçer. İç kenarı evrenden çıkarmak, kablonun iki ucundaki sıranın düz mü
    ters mi olduğunu görünmez kılar - CAM-W örneği bu kaçağın teorik
    olmadığını gösteriyor (J101 pin 17 = XVCLK, J201 pin 17 = FSYNC).
    """

    def __init__(self, edge_id: str, edge_class: str, *,
                 chain_id: Optional[str] = None,
                 contract_link: Optional[str] = None,
                 side_a: Optional[dict] = None,
                 side_b: Optional[dict] = None,
                 medium: Optional[dict] = None,
                 link_faces: Optional[Tuple[Optional[dict], Optional[dict]]] = None,
                 mapping: Optional[dict] = None,
                 pin_to_pin: Optional[list] = None,
                 provenance: Optional[dict] = None,
                 missing_record: bool = False,
                 detail: Optional[str] = None):
        self.edge_id = edge_id
        self.edge_class = edge_class
        self.chain_id = chain_id
        self.contract_link = contract_link
        self.side_a = side_a
        self.side_b = side_b
        self.medium = medium
        self.link_faces = link_faces or (None, None)
        self.mapping = mapping
        self.pin_to_pin = list(pin_to_pin or [])
        self.provenance = provenance or {}
        self.missing_record = missing_record
        self.detail = detail
        self.excluded = False
        self.exclusion_reason: Optional[str] = None
        self.rule_results: List[dict] = []
        self.status = NO_COVERAGE
        self.codes: List[str] = []
        self.caps: List[dict] = []

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "edge_class": self.edge_class,
            "chain_id": self.chain_id,
            "contract_link": self.contract_link,
            "status": self.status,
            "excluded": self.excluded,
            "exclusion_reason": self.exclusion_reason,
            "missing_record": self.missing_record,
            "sides": [_side_summary(self.side_a), _side_summary(self.side_b)],
            "medium": _medium_summary(self.medium),
            "mapping": (self.mapping or {}).get("mapping")
            if self.mapping else None,
            "pin_to_pin_rows": len(self.pin_to_pin),
            "rule_results": self.rule_results,
            "reason_codes": sorted(set(self.codes)),
            "detail": self.detail,
        }


def _side_summary(side: Optional[dict]) -> Optional[dict]:
    if side is None:
        return None
    keys = ("kind", "id", "node_id", "refdes", "interface_type",
            "interface_class", "gender", "mpn", "manufacturer", "revision",
            "pitch_mm", "positions", "contact_face", "pin1_reference",
            "numbering_direction", "contact_finish", "mating_cycles",
            "stack_height_mm", "lane_count", "declared_nonmatching")
    out = {k: side.get(k) for k in keys}
    out["pin_map_size"] = len(side.get("pin_map") or [])
    return out


def _medium_summary(medium: Optional[dict]) -> Optional[dict]:
    if medium is None:
        return None
    keys = ("kind", "mpn", "manufacturer", "revision", "pitch_mm",
            "conductors", "contact_orientation", "vendor_contact_letter",
            "thickness_mm", "width_mm", "margin_mm", "stiffener_face",
            "shielded", "contact_finish", "mating_cycles", "mapping",
            "bom_line")
    return {k: medium.get(k) for k in keys}


def face_side(node: dict, face: dict) -> dict:
    """Bir düğüm yüzünü karşılaştırılabilir bir 'taraf'a normalize et."""
    return {
        "kind": "face",
        "id": _text(face.get("face_id")),
        "node_id": _text(node.get("node_id")),
        "node_kind": _text(node.get("kind")),
        "refdes": _text(face.get("refdes")),
        "interface_type": _text(face.get("interface_type")),
        "interface_class": _text(face.get("interface_class")) or "UNKNOWN",
        "gender": _text(face.get("gender")),
        "mpn": _text(face.get("mpn")),
        "manufacturer": _text(face.get("manufacturer")),
        "revision": _text(face.get("revision")),
        "pitch_mm": _num(face.get("pitch_mm")),
        "positions": _int(face.get("positions")),
        "contact_face": _text(face.get("contact_face")),
        "pin1_reference": _text(face.get("pin1_reference")),
        "numbering_direction": _text(face.get("numbering_direction")),
        "accepts_thickness_mm": face.get("accepts_thickness_mm") or {},
        "contact_finish": _text(face.get("contact_finish")),
        "mating_cycles": _int(face.get("mating_cycles")),
        "stack_height_mm": _num(face.get("stack_height_mm")),
        "lane_count": _int(face.get("lane_count")),
        "valid_mating_mpns": list(face.get("valid_mating_mpns") or []),
        "declared_nonmatching": bool(face.get("declared_nonmatching")),
        "declared_nonmatching_note": _text(face.get("declared_nonmatching_note")),
        "pin_map": list(face.get("pin_map") or []),
        "provenance": face.get("provenance") or {},
    }


def medium_side(medium: dict, end: int) -> dict:
    """Medium'un bir UCUNU karşılaştırılabilir bir 'taraf'a normalize et.

    Bir kablo iki eşleşme yaratır ve kendi düz/ters eşlemesi AYRI bir sorudur;
    bu yüzden iki uç iki ayrı taraftır ve aralarındaki iç kenar üçüncü bir
    kayıttır. ``conductors`` burada ``positions``'a düşer - R4'ün
    "positions vs conductors" karşılaştırması budur.
    """
    stiffener = list(medium.get("stiffener_face") or [])
    return {
        "kind": "medium_end",
        "id": f"medium:end{'ab'[end]}",
        "node_id": None,
        "node_kind": None,
        "refdes": None,
        "interface_type": _text(medium.get("kind")),
        "interface_class": "UNKNOWN",
        "gender": None,
        "mpn": _text(medium.get("mpn")),
        "manufacturer": _text(medium.get("manufacturer")),
        "revision": _text(medium.get("revision")),
        "pitch_mm": _num(medium.get("pitch_mm")),
        "positions": _int(medium.get("conductors")),
        "contact_face": None,
        "stiffener_face": _text(stiffener[end]) if len(stiffener) > end else None,
        "pin1_reference": None,
        "numbering_direction": None,
        "accepts_thickness_mm": {},
        "thickness_mm": _num(medium.get("thickness_mm")),
        "width_mm": _num(medium.get("width_mm")),
        "margin_mm": _num(medium.get("margin_mm")),
        "contact_finish": _text(medium.get("contact_finish")),
        "mating_cycles": _int(medium.get("mating_cycles")),
        "stack_height_mm": None,
        "lane_count": None,
        "valid_mating_mpns": [],
        "declared_nonmatching": False,
        "declared_nonmatching_note": None,
        "pin_map": [],
        # ``conductors``'ın provenansı ``positions`` adı altında da görünür:
        # aynı büyüklük iki adla yaşarsa provenans denetimi sessizce boşa
        # düşer ve karşılaştırılmış bir alan kaynaksız kalır.
        "provenance": {**(medium.get("provenance") or {}),
                       "positions": (medium.get("provenance") or {}).get(
                           "conductors")},
    }


def build_universe(chains: Sequence[dict], contract_links: Sequence[ContractLink]
                   ) -> Tuple[List[Edge], List[dict], List[dict], dict]:
    """(kenarlar, bağlanmayan zincirler, karşılığı olmayan sözleşme bağları,
    payda türetme kaydı).

    Payda BURADA türetilir ve türetmenin izi rapora yazılır. Zincirin kendi
    beyan ettiği kenar sayısı da yazılır ama HİÇBİR yerde payda olarak
    kullanılmaz - iki sayının yan yana durması, birinin diğerinin yerine
    geçtiği anı görünür kılar.
    """
    by_ref: Dict[str, dict] = {}
    unbound: List[dict] = []
    for chain in chains:
        link = match_contract_link(_text(chain.get("contract_ref")),
                                   contract_links)
        if link is None:
            unbound.append({
                "chain_id": chain.get("chain_id"),
                "contract_ref": chain.get("contract_ref"),
                "issue": CONTRACT_REF_UNRESOLVED,
                "detail": "contract_ref sözleşmedeki hiçbir zorunlu bağlantıya "
                          "çözülmüyor; bu zincirin kenarları evrene GİRMEZ ve "
                          "zincir bir ihlal olarak raporlanır"})
            continue
        if link.link_id in by_ref:
            unbound.append({
                "chain_id": chain.get("chain_id"),
                "contract_ref": chain.get("contract_ref"),
                "issue": CONTRACT_REF_UNRESOLVED,
                "detail": f"{link.link_id!r} sözleşme bağına ikinci bir zincir "
                          "bağlanmış; hangisinin ölçüldüğü sıraya bağlı olurdu"})
            continue
        by_ref[link.link_id] = chain

    edges: List[Edge] = []
    seen_edge_ids: set = set()
    unmatched: List[dict] = []
    per_link: List[dict] = []
    declared_edges = 0

    def add(edge: Edge) -> bool:
        if edge.edge_id in seen_edge_ids:
            return False          # aynı kart iki zincirde: iç kenar TEK fakttır
        seen_edge_ids.add(edge.edge_id)
        edges.append(edge)
        return True

    for link in contract_links:
        chain = by_ref.get(link.link_id)
        before = len(edges)
        if chain is None:
            # SÖZLEŞME ASGARİSİ: zorunlu bir arayüzün en az bir fiziksel
            # eşleşme geçişi vardır. Zinciri olmayan arayüz evrenden DÜŞMEZ;
            # asgari kenarıyla durur ve MISSING_TRANSITION ile ihlal sayılır.
            edge = Edge(f"missing:{link.link_id}", "mating",
                        chain_id=None, contract_link=link.link_id,
                        missing_record=True,
                        detail="sözleşme bu arayüzü gerektiriyor ama hiçbir "
                               "zincir ona bağlanmamış")
            add(edge)
            unmatched.append({"link_id": link.link_id,
                              "pointer": link.pointer,
                              "issue": MISSING_TRANSITION,
                              "connector_refdes":
                                  link.detail.get("connector_refdes")})
            per_link.append({"link_id": link.link_id, "pointer": link.pointer,
                             "chain_id": None, "edge_quota": 1,
                             "basis": "contract minimum (zincir yok)"})
            continue

        chain_id = _text(chain.get("chain_id"))
        nodes = {_text(n.get("node_id")): n for n in chain["nodes"]
                 if isinstance(n, dict)}

        # (a) İÇ KENARLAR: düğüm başına yüz_sayısı - 1
        for node in chain["nodes"]:
            node_id = _text(node.get("node_id"))
            faces = [f for f in (node.get("faces") or []) if isinstance(f, dict)]
            mappings = {(_text(m.get("from_face")), _text(m.get("to_face"))): m
                        for m in (node.get("internal_mappings") or [])
                        if isinstance(m, dict)}
            for index in range(len(faces) - 1):
                a, b = faces[index], faces[index + 1]
                key = (_text(a.get("face_id")), _text(b.get("face_id")))
                mapping = mappings.get(key) or mappings.get((key[1], key[0]))
                edge = Edge(f"int:{node_id}:{key[0]}>{key[1]}", "internal",
                            chain_id=chain_id, contract_link=link.link_id,
                            side_a=face_side(node, a), side_b=face_side(node, b),
                            mapping=mapping,
                            pin_to_pin=(mapping or {}).get("pin_to_pin") or [],
                            provenance=(mapping or {}).get("provenance") or {},
                            detail=f"{node_id} iç geçişi "
                                   f"{key[0]} -> {key[1]}")
                add(edge)

        # (b) DIŞ KENARLAR + medium iç kenarı
        for ic in chain["interconnects"]:
            if not isinstance(ic, dict):
                raise InterconnectMatingError(
                    f"chain: {chain_id}: bir interconnect nesne değil")
            link_id = _text(ic.get("link_id"))
            if link_id is None:
                raise InterconnectMatingError(
                    f"chain: {chain_id}: bir interconnect 'link_id' taşımıyor")
            endpoints = ic.get("endpoints") or []
            if not isinstance(endpoints, list) or len(endpoints) != 2:
                raise InterconnectMatingError(
                    f"chain: {chain_id}/{link_id}: 'endpoints' tam iki uç "
                    "taşımalı; bir ucu eksik olan halka hiçbir şeyi bağlamaz")
            faces: List[Optional[dict]] = []
            for end in endpoints:
                node = nodes.get(_text((end or {}).get("node_id")))
                face = None
                if node is not None:
                    for candidate in node.get("faces") or []:
                        if isinstance(candidate, dict) and \
                                _text(candidate.get("face_id")) == \
                                _text((end or {}).get("face_id")):
                            face = face_side(node, candidate)
                            break
                faces.append(face)
            medium = ic.get("medium") if isinstance(ic.get("medium"), dict) else None
            kind = _text((medium or {}).get("kind"))
            two_ended = medium is not None and kind != DIRECT_MATE
            declared_edges += (3 if two_ended else 1)

            if medium is None or kind == DIRECT_MATE:
                # Medium yoksa kenar YİNE VAR: asgari bir eşleşme kenarı
                # olarak sayılır, taranır ve R1 ile ihlal verir. Eksik halkayı
                # evrenden düşürmek, tam olarak bu kapının engellediği hamledir.
                add(Edge(f"mate:{link_id}", "mating", chain_id=chain_id,
                         contract_link=link.link_id,
                         side_a=faces[0], side_b=faces[1], medium=medium,
                         link_faces=(faces[0], faces[1]),
                         pin_to_pin=ic.get("pin_to_pin") or [],
                         provenance=ic.get("provenance") or {},
                         detail=f"{link_id}: doğrudan eşleşme"
                                if medium is not None else
                                f"{link_id}: medium BEYAN EDİLMEMİŞ"))
            else:
                add(Edge(f"mate:{link_id}:a", "mating", chain_id=chain_id,
                         contract_link=link.link_id,
                         side_a=faces[0], side_b=medium_side(medium, 0),
                         medium=medium, link_faces=(faces[0], faces[1]),
                         pin_to_pin=ic.get("pin_to_pin") or [],
                         provenance=ic.get("provenance") or {},
                         detail=f"{link_id}: A ucu eşleşmesi"))
                add(Edge(f"mate:{link_id}:b", "mating", chain_id=chain_id,
                         contract_link=link.link_id,
                         side_a=medium_side(medium, 1), side_b=faces[1],
                         medium=medium, link_faces=(faces[0], faces[1]),
                         pin_to_pin=ic.get("pin_to_pin") or [],
                         provenance=ic.get("provenance") or {},
                         detail=f"{link_id}: B ucu eşleşmesi"))
                # MEDIUM İÇ KENARI. Tarafları kablonun UÇLARI değil, linkin iki
                # KART YÜZÜ'dür - ve bu bir kolaylık değil, sorunun kendisidir:
                # kablonun ucunda net adı yoktur, net adları kartlardan gelir.
                # Ölçülen şey "kablonun beyan ettiği düz/ters eşleme, doğru
                # netleri doğru pinlere düşürüyor mu"dur. Bu kenarı çıkarmak,
                # kablonun iki ucundaki sıranın ters olduğunu görünmez kılar.
                add(Edge(f"int:{link_id}:medium", "internal", chain_id=chain_id,
                         contract_link=link.link_id,
                         side_a=faces[0], side_b=faces[1],
                         medium=medium,
                         link_faces=(faces[0], faces[1]),
                         mapping={"from_face": "medium:enda",
                                  "to_face": "medium:endb",
                                  "mapping": _text(medium.get("mapping")),
                                  "pin_to_pin": medium.get("pin_to_pin") or [],
                                  "provenance": medium.get("provenance") or {}},
                         pin_to_pin=medium.get("pin_to_pin") or [],
                         provenance=medium.get("provenance") or {},
                         detail=f"{link_id}: medium iç eşlemesi "
                                "(düz / ters / custom)"))

        # SÖZLEŞME ASGARİSİ, ZİNCİR VARKEN DE GEÇERLİ. Bağlı bir zincirin hiç
        # eşleşme kenarı üretmemesi (interconnects boş, ya da her düğüm tek
        # yüzlü) paydayı sıfıra indirirdi ve `scanned=0 -> NO_COVERAGE` bir
        # RED'in yerini alırdı. Beyanı silmek paydayı küçültmez: zorunlu bir
        # arayüzün EN AZ BİR fiziksel eşleşme geçişi vardır.
        if not any(e.contract_link == link.link_id and e.edge_class == "mating"
                   for e in edges[before:]):
            add(Edge(f"missing:{link.link_id}", "mating", chain_id=chain_id,
                     contract_link=link.link_id, missing_record=True,
                     detail="zincir bağlı ama hiç eşleşme kenarı bildirmiyor; "
                            "sözleşme asgarisi evrende duruyor"))
            unmatched.append({"link_id": link.link_id,
                              "pointer": link.pointer,
                              "chain_id": chain_id,
                              "issue": MISSING_TRANSITION,
                              "connector_refdes":
                                  link.detail.get("connector_refdes"),
                              "detail": "bağlı zincirde eşleşme kenarı yok"})

        quota = len(edges) - before
        per_link.append({"link_id": link.link_id, "pointer": link.pointer,
                         "chain_id": chain_id, "edge_quota": quota,
                         "basis": "Sigma_düğüm (yüz - 1) + dış_kenar "
                                  "(asgari 1 eşleşme kenarı)"})

    derivation = {
        "source": "interface_contract.json zorunlu bağlantı kümesi",
        "formula": "eligible = Sigma_düğüm (yüz_sayısı - 1) + dış_kenar_sayısı",
        "flat_chain_note": "düz zincirde bu tam olarak 'düğüm - 1'e indirgenir",
        "required_links": per_link,
        "chain_declared_edge_count": declared_edges,
        "chain_declared_is_not_the_denominator":
            "beyan edilen kenar sayısı bilgilendirme amaçlıdır. Payda "
            "sözleşmeden gelir; iki sayı yan yana durur ki birinin diğerinin "
            "yerine geçtiği an görünür olsun",
        "deduplicated_internal_edges":
            "aynı kart iki zincirde geçse bile iç kenarı BİR kez sayılır: "
            "J101 <-> J201 sürekliliği tek bir fakttır",
    }
    return edges, unbound, unmatched, derivation


# --------------------------------------------------------------------------- #
# 4. Kurallar R1..R20
# --------------------------------------------------------------------------- #

def _rule(rule: str, title: str, status: str, authority: str, level: str,
          codes: Optional[Sequence[str]] = None, detail: str = "") -> dict:
    return {"rule": rule, "title": title, "status": status,
            "authority": authority, "level": level,
            "codes": sorted(set(codes or ())), "detail": detail}


def _both(a: Any, b: Any) -> bool:
    return a is not None and b is not None


def _pin_index(pin_map: Sequence[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for entry in pin_map:
        if isinstance(entry, dict):
            pin = _text(entry.get("pin"))
            if pin is not None:
                out[pin] = entry
    return out


# ---- R1 / R18: parça var mı, belirli mi ----------------------------------- #

def r1_medium_present(edge: Edge) -> dict:
    title = "medium mevcut"
    if edge.missing_record:
        return _rule("R1", title, FAIL, SIGNOFF, "C->A", [MISSING_TRANSITION],
                     "sözleşme bu arayüzü gerektiriyor, hiçbir zincir kaydı yok")
    if edge.edge_class != "mating":
        return _rule("R1", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "iç kenarda medium sorusu yok")
    if edge.medium is None:
        return _rule("R1", title, FAIL, SIGNOFF, "C->A", [NO_MATED_PART],
                     "medium BEYAN EDİLMEMİŞ: iki uç arasındaki mesafeyi "
                     "kapatan cable assembly / adaptör tanımsız")
    return _rule("R1", title, PASS, SIGNOFF, "C->A", [],
                 f"medium.kind={edge.medium.get('kind')}")


def r18_determinacy(edge: Edge) -> dict:
    title = "parça belirlenmişlik (mpn + manufacturer + revision)"
    sides = [s for s in (edge.side_a, edge.side_b) if s is not None]
    if not sides:
        return _rule("R18", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "karşılaştırılacak taraf yok")
    gaps: List[str] = []
    for side in sides:
        missing = [f for f in ("mpn", "manufacturer", "revision")
                   if side.get(f) is None]
        if missing:
            gaps.append(f"{side.get('id')}: {', '.join(missing)} eksik")
    if gaps:
        return _rule("R18", title, FAIL, SIGNOFF, "C->A", [BLOCKED_UNSPECIFIED],
                     "; ".join(gaps))
    return _rule("R18", title, PASS, SIGNOFF, "C->A", [],
                 "iki taraf da MPN + üretici + revizyon taşıyor")


# ---- R2..R9: fiziksel takılabilirlik ------------------------------------- #

def r2_interface_class(edge: Edge) -> dict:
    title = "arayüz sınıfı uyumu"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "iki taraf çözülmedi")
    kind = _text((edge.medium or {}).get("kind"))
    if kind is None:
        return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "medium.kind yok; hangi sınıfların köprülendiği bilinmiyor")
    if kind == DIRECT_MATE:
        if "UNKNOWN" in (a["interface_class"], b["interface_class"]):
            return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                         "en az bir tarafın arayüz sınıfı UNKNOWN")
        if a["interface_class"] != b["interface_class"]:
            return _rule("R2", title, FAIL, SIGNOFF, "C->A",
                         [INTERFACE_CLASS_MISMATCH],
                         f"doğrudan eşleşmede {a['interface_class']} ile "
                         f"{b['interface_class']} karşı karşıya")
        return _rule("R2", title, PASS, SIGNOFF, "C->A", [],
                     f"iki taraf da {a['interface_class']}")
    compat = MEDIUM_CLASS_COMPAT.get(kind)
    if compat is None:
        return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     f"medium.kind={kind!r} için sınıf uyum tablosu yok")
    face = a if a["kind"] == "face" else (b if b["kind"] == "face" else None)
    if face is None:
        return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "kenarın iki tarafı da medium ucu")
    if face["interface_class"] == "UNKNOWN":
        return _rule("R2", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     f"{face['id']} arayüz sınıfı UNKNOWN")
    if face["interface_class"] not in compat:
        return _rule("R2", title, FAIL, SIGNOFF, "C->A",
                     [INTERFACE_CLASS_MISMATCH],
                     f"{kind} bir {face['interface_class']} yüzeyini köprülemez")
    return _rule("R2", title, PASS, SIGNOFF, "C->A", [],
                 f"{kind} <-> {face['interface_class']}")


def r3_pitch(edge: Edge) -> dict:
    title = "pitch eşitliği"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R3", title, NO_COVERAGE, SIGNOFF, "C->A", [], "taraf yok")
    values = [("A", a.get("pitch_mm")), ("B", b.get("pitch_mm"))]
    medium_pitch = _num((edge.medium or {}).get("pitch_mm"))
    if medium_pitch is not None:
        values.append(("medium", medium_pitch))
    known = [(n, v) for n, v in values if v is not None]
    if len(known) < 2:
        return _rule("R3", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "karşılaştırmak için en az iki ölçülmüş pitch gerekiyor; "
                     "null bir değer eşit sayılmaz")
    distinct = {round(v, 6) for _, v in known}
    if len(distinct) > 1:
        return _rule("R3", title, FAIL, SIGNOFF, "C->A", [PITCH_MISMATCH],
                     "pitch: " + ", ".join(f"{n}={v:g} mm" for n, v in known))
    return _rule("R3", title, PASS, SIGNOFF, "C->A", [],
                 f"tümü {known[0][1]:g} mm")


def r4_positions(edge: Edge) -> dict:
    """SCREEN_ONLY - imza AÇMAZ.

    Bu, ``ecad-varlik-kalifikasyonu``'nun tüm varlık sebebinin zincire
    uygulanmasıdır: 20 pozisyonlu breakout + 20 pozisyonlu FPC "20=20" geçer,
    ama pin 1 karşı uçta pin 20'ye düşüyorsa zincir yanlıştır. Temiz bir R4
    imzayı açmaz; imzayı R10 + R11 taşır. Bir FAIL yine de kapıyı kapatır:
    30 kontaklı kablo 22 kontaklı yuvaya girmez.
    """
    title = "pozisyon sayısı eşitliği (positions vs conductors)"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R4", title, NO_COVERAGE, SCREEN, "C->A", [], "taraf yok")
    pa, pb = a.get("positions"), b.get("positions")
    if not _both(pa, pb):
        return _rule("R4", title, NO_COVERAGE, SCREEN, "C->A", [],
                     "en az bir tarafın pozisyon/iletken sayısı ölçülmemiş")
    if pa != pb:
        return _rule("R4", title, FAIL, SCREEN, "C->A",
                     [POSITION_COUNT_MISMATCH],
                     f"{a['id']}={pa} kontak, {b['id']}={pb} kontak")
    return _rule("R4", title, PASS, SCREEN, "C->A", [],
                 f"{pa} = {pb}; SAYI EŞİTLİĞİ KİMLİK EŞİTLİĞİ DEĞİLDİR - "
                 "bu PASS imza açmaz")


def r5_gender(edge: Edge) -> dict:
    title = "cinsiyet"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R5", title, NO_COVERAGE, SIGNOFF, "C->A", [], "taraf yok")
    ga, gb = a.get("gender"), b.get("gender")
    if not _both(ga, gb) or "UNKNOWN" in (ga, gb):
        return _rule("R5", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "en az bir tarafın cinsiyeti beyan edilmemiş "
                     "(kablo ucu cinsiyeti şemada taşınmaz)")
    if (ga, gb) not in GENDER_OK:
        return _rule("R5", title, FAIL, SIGNOFF, "C->A", [GENDER_CONFLICT],
                     f"{ga} <-> {gb}")
    return _rule("R5", title, PASS, SIGNOFF, "C->A", [], f"{ga} <-> {gb}")


def r6_contact_face(edge: Edge) -> dict:
    """Kontak yüzü (M1). Sertleştirici, konnektörün kontak yüzünün KARŞISINDA
    olmak zorundadır; aynı yüzde ise kontak hiç değmez."""
    title = "kontak yüzü (stiffener, kontağın karşı yüzünde)"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R6", title, NO_COVERAGE, RECORD, "C", [], "taraf yok")
    face = a if a["kind"] == "face" else (b if b["kind"] == "face" else None)
    end = b if a["kind"] == "face" else (a if b["kind"] == "face" else None)
    if face is None or end is None or end["kind"] != "medium_end":
        return _rule("R6", title, NO_COVERAGE, RECORD, "C", [],
                     "kenar bir yüz + bir medium ucu çifti değil")
    contact = face.get("contact_face")
    stiffener = end.get("stiffener_face")
    if not _both(contact, stiffener) or contact in ("BOTH", "NA") \
            or stiffener == "NONE":
        return _rule("R6", title, NO_COVERAGE, RECORD, "C", [],
                     "contact_face / stiffener_face ölçülmemiş; hiçbir kart "
                     "dosyasında bu alan yok - geriye dönük doldurma işi")
    if OPPOSITE_SIDE_OF.get(contact) != stiffener:
        return _rule("R6", title, FAIL, RECORD, "C", [CONTACT_FACE_MISMATCH],
                     f"{face['id']} kontakları {contact}, sertleştirici "
                     f"{stiffener}: kontak yüzü sertleştiriciye bakıyor")
    return _rule("R6", title, PASS, RECORD, "C", [],
                 f"kontak {contact}, sertleştirici {stiffener}")


def r7_cable_type(edge: Edge) -> dict:
    """Kablo tipi tutarlılığı (M3): ``contact_orientation`` iki ucun
    sertleştirici yüzüyle tutarlı olmak zorundadır. Satıcı harfi (Molex A/D,
    Würth 1/2) HİÇBİR kararı beslemez; yalnız normalize enum okunur."""
    title = "kablo tipi tutarlılığı (contact_orientation <-> stiffener çifti)"
    medium = edge.medium
    if edge.edge_class != "mating" or medium is None:
        return _rule("R7", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "medium yok")
    orientation = _text(medium.get("contact_orientation"))
    stiffener = [_text(s) for s in (medium.get("stiffener_face") or [])]
    if orientation is None or orientation == "NA" or len(stiffener) != 2 \
            or None in stiffener:
        return _rule("R7", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "contact_orientation veya iki uçlu stiffener_face "
                     "beyan edilmemiş")
    expected = "SAME_SIDE" if stiffener[0] == stiffener[1] else "OPPOSITE_SIDE"
    if orientation != expected:
        return _rule("R7", title, FAIL, SIGNOFF, "C->A", [CABLE_TYPE_MISMATCH],
                     f"stiffener {stiffener} -> {expected} beklenirdi, "
                     f"beyan {orientation}")
    return _rule("R7", title, PASS, SIGNOFF, "C->A", [],
                 f"{orientation} beyanı stiffener çiftiyle tutarlı")


def r8_tail_thickness(edge: Edge) -> dict:
    title = "kuyruk kalınlığı yuvanın kabul aralığında"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R8", title, NO_COVERAGE, RECORD, "C", [], "taraf yok")
    face = a if a["kind"] == "face" else (b if b["kind"] == "face" else None)
    end = b if a["kind"] == "face" else (a if b["kind"] == "face" else None)
    if face is None or end is None or end["kind"] != "medium_end":
        return _rule("R8", title, NO_COVERAGE, RECORD, "C", [], "çift değil")
    accepts = face.get("accepts_thickness_mm") or {}
    nom, tol = _num(accepts.get("nom")), _num(accepts.get("tol"))
    thickness = end.get("thickness_mm")
    if nom is None or thickness is None:
        return _rule("R8", title, NO_COVERAGE, RECORD, "C", [],
                     "accepts_thickness_mm.nom veya medium.thickness_mm "
                     "ölçülmemiş")
    band = tol if tol is not None else 0.0
    if not (nom - band <= thickness <= nom + band):
        return _rule("R8", title, FAIL, RECORD, "C",
                     [TAIL_THICKNESS_OUT_OF_RANGE],
                     f"kuyruk {thickness:g} mm, yuva {nom:g} +/- {band:g} mm")
    return _rule("R8", title, PASS, RECORD, "C", [],
                 f"{thickness:g} mm, {nom:g} +/- {band:g} mm bandında")


def r9_tail_geometry(edge: Edge) -> dict:
    """Genişlik & margin. Kontrol edilen şey bir UYDURULMUŞ LİMİT değil, beyan
    edilmiş değerler üzerindeki geometrik kimliktir:
    ``width >= (conductors - 1) * pitch + 2 * margin``."""
    title = "kuyruk genişliği & margin (beyanların geometrik tutarlılığı)"
    medium = edge.medium
    if edge.edge_class != "mating" or medium is None:
        return _rule("R9", title, NO_COVERAGE, SIGNOFF, "C->A", [], "medium yok")
    width = _num(medium.get("width_mm"))
    margin = _num(medium.get("margin_mm"))
    pitch = _num(medium.get("pitch_mm"))
    conductors = _int(medium.get("conductors"))
    if None in (width, margin, pitch) or conductors is None:
        return _rule("R9", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "width_mm / margin_mm / pitch_mm / conductors dördü "
                     "birden gerekiyor")
    needed = (conductors - 1) * pitch + 2 * margin
    if width + 1e-9 < needed:
        return _rule("R9", title, FAIL, SIGNOFF, "C->A",
                     [TAIL_GEOMETRY_MISMATCH],
                     f"genişlik {width:g} mm, gereken en az {needed:g} mm "
                     f"({conductors} iletken x {pitch:g} mm + 2 x {margin:g} mm)")
    return _rule("R9", title, PASS, SIGNOFF, "C->A", [],
                 f"{width:g} mm >= {needed:g} mm")


# ---- R10..R13: pin örtüşmesi (imzanın taşıyıcısı) ----------------------- #

def r10_pinmap_declared(edge: Edge) -> dict:
    title = "pin-to-pin çizelgesi zorunlu"
    if edge.missing_record:
        return _rule("R10", title, FAIL, SIGNOFF, "C->A", [PINMAP_UNDECLARED],
                     "kayıt yok, çizelge de yok")
    if edge.edge_class == "internal":
        mapping = edge.mapping
        declared = _text((mapping or {}).get("mapping"))
        if mapping is None or declared is None:
            return _rule("R10", title, NO_COVERAGE, SIGNOFF, "C->A",
                         [MAPPING_UNDECLARED],
                         "iç kenar straight / reversed / custom BEYAN "
                         "ETMEMİŞ: kablonun iki ucundaki sıranın düz mü ters "
                         "mi olduğu görünmez")
        if declared == "custom" and not edge.pin_to_pin:
            return _rule("R10", title, FAIL, SIGNOFF, "C->A",
                         [PINMAP_UNDECLARED],
                         "'custom' beyan edildi ama pin_to_pin[] boş; "
                         "'custom' tek başına bir bilgi değildir")
        if declared in ("straight", "reversed"):
            return _rule("R10", title, PASS, SIGNOFF, "C->A", [],
                         f"iç eşleme {declared} olarak beyan edildi")
        return _rule("R10", title, PASS, SIGNOFF, "C->A", [],
                     f"custom + {len(edge.pin_to_pin)} satır çizelge")
    if not edge.pin_to_pin:
        return _rule("R10", title, FAIL, SIGNOFF, "C->A", [PINMAP_UNDECLARED],
                     "netin karşı uçta hangi FİZİKSEL PİNE düştüğü beyan "
                     "edilmemiş. RPi 5'te CSI ve DSI portları fiziksel olarak "
                     "özdeştir ve kabloları birbirinin yerine geçmez - "
                     "geometri eşitliği bu çizelgenin yerine geçmez")
    return _rule("R10", title, PASS, SIGNOFF, "C->A", [],
                 f"{len(edge.pin_to_pin)} satır çizelge")


def _pinmap_sides(edge: Edge) -> Tuple[Optional[dict], Optional[dict]]:
    """R10..R13'ün karşılaştırdığı iki taraf.

    Bir eşleşme kenarında geometri iki KOMŞU yüzey arasındadır (yuva <-> kablo
    ucu), ama pin örtüşmesi LİNK boyunca uçtan uca bir sorudur: "J1'deki net X,
    J2'de hangi fiziksel pine düşüyor". Kablonun ucunda net adı yoktur; net
    adları kartlardan gelir. Bu yüzden pin kuralları linkin iki kart yüzünü
    okur, medium ucunu değil - aksi hâlde her kabloda R11/R12/R13 sessizce
    NO_COVERAGE olurdu ve "baktık, temiz" ile "bakamadık" ayırt edilemezdi.
    """
    face_a, face_b = edge.link_faces
    if face_a is not None and face_b is not None:
        return face_a, face_b
    return edge.side_a, edge.side_b


def _resolved_rows(edge: Edge) -> Tuple[List[dict], Dict[str, dict], Dict[str, dict]]:
    side_a, side_b = _pinmap_sides(edge)
    a = _pin_index((side_a or {}).get("pin_map") or [])
    b = _pin_index((side_b or {}).get("pin_map") or [])
    rows = []
    for row in edge.pin_to_pin:
        if not isinstance(row, dict):
            continue
        src, dst = _text(row.get("from_pin")), _text(row.get("to_pin"))
        if src is None or dst is None:
            continue
        rows.append({"from_pin": src, "to_pin": dst,
                     "from": a.get(src), "to": b.get(dst),
                     "declared_net": _text(row.get("net"))})
    return rows, a, b


def _straight_rows(edge: Edge) -> List[dict]:
    """``straight`` / ``reversed`` beyanını çizelgeye AÇAR.

    Beyanı çizelgeye açmadan R11/R12/R13 koşamaz; açmak bir varsayım değil,
    beyanın anlamının kendisidir. ``reversed`` = pin i, karşı uçta N+1-i.
    """
    declared = _text((edge.mapping or {}).get("mapping"))
    if declared not in ("straight", "reversed"):
        return []
    side_a, side_b = _pinmap_sides(edge)
    a = [_text(p.get("pin")) for p in ((side_a or {}).get("pin_map") or [])
         if isinstance(p, dict)]
    b = [_text(p.get("pin")) for p in ((side_b or {}).get("pin_map") or [])
         if isinstance(p, dict)]
    a = [p for p in a if p is not None]
    b = [p for p in b if p is not None]
    if not a or len(a) != len(b):
        return []
    order = b if declared == "straight" else list(reversed(b))
    return [{"from_pin": src, "to_pin": dst, "net": None}
            for src, dst in zip(a, order)]


def r11_net_identity(edge: Edge) -> dict:
    """Net kimliği - ve KÜME EŞİTLİĞİ ile SIRA EŞİTLİĞİNİN ayrımı.

    Aynı netler yanlış sırada ise kod ``PIN_ORDER_MISMATCH``'dir: küme eşitliği
    ``ecad-varlik-kalifikasyonu``'nun, SIRA bu kapının predicate'idir.
    Ad karşılaştırması birebirdir; alias tablosu YOKTUR.
    """
    title = "net kimliği (birebir, alias tablosu yok)"
    rows, _, _ = _resolved_rows(edge)
    if not rows:
        return _rule("R11", title, NO_COVERAGE, SIGNOFF, "A", [],
                     "çözülebilir çizelge satırı yok")
    compared = [r for r in rows if r["from"] is not None and r["to"] is not None]
    if not compared:
        return _rule("R11", title, NO_COVERAGE, SIGNOFF, "A", [],
                     "çizelge satırları hiçbir pin haritasına çözülmüyor")
    bad = []
    for row in compared:
        left = _text((row["from"] or {}).get("net"))
        right = _text((row["to"] or {}).get("net"))
        if left is None or right is None:
            continue
        if left != right:
            bad.append({"from_pin": row["from_pin"], "to_pin": row["to_pin"],
                        "from_net": left, "to_net": right})
    if not bad:
        return _rule("R11", title, PASS, SIGNOFF, "A", [],
                     f"{len(compared)} satırın net adları birebir örtüşüyor")
    left_nets = sorted(_text((r["from"] or {}).get("net")) or "" for r in compared)
    right_nets = sorted(_text((r["to"] or {}).get("net")) or "" for r in compared)
    codes = [NET_NAME_MISMATCH]
    if left_nets == right_nets:
        codes.append(PIN_ORDER_MISMATCH)
    sample = "; ".join(f"{v['from_pin']}({v['from_net']}) -> "
                       f"{v['to_pin']}({v['to_net']})" for v in bad[:4])
    return _rule("R11", title, FAIL, SIGNOFF, "A", codes,
                 f"{len(bad)} satır uyuşmuyor: {sample}"
                 + (" | AYNI NET KÜMESİ, YANLIŞ SIRA" if PIN_ORDER_MISMATCH
                    in codes else ""))


def r12_power_on_signal(edge: Edge) -> dict:
    """ÖLDÜRÜCÜ KURAL. R6 sessiz açık devre üretir; bu sessiz HASAR üretir:
    3V3 rayının bir D-PHY lane'ine binmesi sensör/ISP girişini kalıcı olarak
    bozar. İkisi aynı önceliğe konmaz."""
    title = "güç/veri çarpışması (yanlış pin = KALICI HASAR)"
    rows, _, _ = _resolved_rows(edge)
    compared = [r for r in rows if r["from"] is not None and r["to"] is not None]
    if not compared:
        return _rule("R12", title, NO_COVERAGE, SIGNOFF, "A", [],
                     "sınıf bilgisiyle çözülen satır yok")
    bad = []
    for row in compared:
        left = _text((row["from"] or {}).get("class"))
        right = _text((row["to"] or {}).get("class"))
        if left is None or right is None:
            continue
        if (left in RAIL_CLASSES and right in SIGNAL_CLASSES) or \
                (right in RAIL_CLASSES and left in SIGNAL_CLASSES):
            bad.append({"from_pin": row["from_pin"], "to_pin": row["to_pin"],
                        "from_class": left, "to_class": right,
                        "from_net": _text((row["from"] or {}).get("net")),
                        "to_net": _text((row["to"] or {}).get("net"))})
    scanned = [r for r in compared
               if _text((r["from"] or {}).get("class")) is not None
               and _text((r["to"] or {}).get("class")) is not None]
    if not scanned:
        return _rule("R12", title, NO_COVERAGE, SIGNOFF, "A", [],
                     "hiçbir satırda iki taraflı pin sınıfı beyan edilmemiş")
    if bad:
        sample = "; ".join(f"{v['from_pin']}({v['from_class']}/{v['from_net']}) "
                           f"-> {v['to_pin']}({v['to_class']}/{v['to_net']})"
                           for v in bad[:4])
        return _rule("R12", title, FAIL, SIGNOFF, "A", [POWER_ON_SIGNAL_PIN],
                     f"{len(bad)} ray/sinyal çarpışması: {sample}")
    return _rule("R12", title, PASS, SIGNOFF, "A", [],
                 f"{len(scanned)} satırda ray hiçbir sinyal pinine binmiyor")


def r13_diffpair(edge: Edge) -> dict:
    title = "diferansiyel çift bütünlüğü"
    rows, _, _ = _resolved_rows(edge)
    compared = [r for r in rows if r["from"] is not None and r["to"] is not None]
    pairs = [r for r in compared
             if _text((r["from"] or {}).get("class")) in DIFF_CLASSES
             and _text((r["to"] or {}).get("class")) in DIFF_CLASSES]
    if not pairs:
        return _rule("R13", title, NO_COVERAGE, SIGNOFF, "A", [],
                     "iki tarafta da diferansiyel sınıflı pin çözülmedi")
    swaps, splits = [], []
    for row in pairs:
        left_cls = _text((row["from"] or {}).get("class"))
        right_cls = _text((row["to"] or {}).get("class"))
        left_pair = _text((row["from"] or {}).get("pair"))
        right_pair = _text((row["to"] or {}).get("pair"))
        if (left_cls in POSITIVE_CLASSES) != (right_cls in POSITIVE_CLASSES):
            swaps.append({"from_pin": row["from_pin"], "to_pin": row["to_pin"],
                          "from_class": left_cls, "to_class": right_cls})
        if _both(left_pair, right_pair) and left_pair != right_pair:
            splits.append({"from_pin": row["from_pin"], "to_pin": row["to_pin"],
                           "from_pair": left_pair, "to_pair": right_pair})
    codes = ([DIFFPAIR_SWAP] if swaps else []) + \
            ([DIFFPAIR_SPLIT] if splits else [])
    if codes:
        return _rule("R13", title, FAIL, SIGNOFF, "A", codes,
                     f"{len(swaps)} P/N takası, {len(splits)} çift ayrışması")
    return _rule("R13", title, PASS, SIGNOFF, "A", [],
                 f"{len(pairs)} diferansiyel satır polarite ve çift adıyla "
                 "örtüşüyor")


# ---- R14..R20: aile, provenans, kayıt ----------------------------------- #

def r14_lane_count(edge: Edge) -> dict:
    """Lane sayısı UÇTAN UCA bir mimari özelliktir, komşu yüzeyler arası değil:
    kablonun ucu lane bilmez. İki KART yüzü karşılaştırılır - aksi hâlde her
    kabloda R14 sessizce NO_COVERAGE olurdu ve tetikleyici vakanın kendisi
    (Luckfox konnektörü 2-lane, OG05B10 4-lane) hiç görünmezdi."""
    title = "lane sayısı"
    a, b = _pinmap_sides(edge)
    if a is None or b is None:
        return _rule("R14", title, NO_COVERAGE, SIGNOFF, "C->A", [], "taraf yok")
    la, lb = a.get("lane_count"), b.get("lane_count")
    if not _both(la, lb):
        return _rule("R14", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "en az bir tarafın lane sayısı beyan edilmemiş")
    if la != lb:
        return _rule("R14", title, FAIL, SIGNOFF, "C->A", [LANE_COUNT_MISMATCH],
                     f"{a['id']}={la} lane, {b['id']}={lb} lane: fiziksel kapı "
                     "geçilse bile mimari geçersiz")
    return _rule("R14", title, PASS, SIGNOFF, "C->A", [], f"{la} lane")


def r15_stack_height(edge: Edge) -> dict:
    title = "mezzanine stack height"
    a, b = edge.side_a, edge.side_b
    if a is None or b is None:
        return _rule("R15", title, NO_COVERAGE, SIGNOFF, "C->A", [], "taraf yok")
    ha, hb = a.get("stack_height_mm"), b.get("stack_height_mm")
    if not _both(ha, hb):
        return _rule("R15", title, NO_COVERAGE, SIGNOFF, "C->A", [],
                     "stack_height_mm iki tarafta birden beyan edilmemiş")
    if abs(ha - hb) > 1e-9:
        return _rule("R15", title, FAIL, SIGNOFF, "C->A",
                     [STACK_HEIGHT_MISMATCH], f"{ha:g} mm vs {hb:g} mm")
    return _rule("R15", title, PASS, SIGNOFF, "C->A", [], f"{ha:g} mm")


def r16_valid_mating(edge: Edge) -> dict:
    """RECORD_ONLY, ve bilinçli olarak. Üreticiler 'mates with' ilişkisini
    makine-okunur yayımlamıyor; ölçülmüş bir vakada Molex'in kendi datasheet'i
    kendi ordering table'ıyla çelişiyor. Bu bir İNSAN GİRİŞİDİR."""
    title = "mating half MPN beyan edilen listede"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R16", title, NO_COVERAGE, RECORD, "C", [], "taraf yok")
    checks = []
    for side, other in ((a, b), (b, a)):
        allowed = [x for x in (side.get("valid_mating_mpns") or [])]
        if allowed and other.get("mpn") is not None:
            checks.append((side, other, allowed, other["mpn"] in allowed))
    if not checks:
        return _rule("R16", title, NO_COVERAGE, RECORD, "C", [],
                     "valid_mating_mpns boş ya da karşı MPN yok")
    bad = [c for c in checks if not c[3]]
    if bad:
        side, other, allowed, _ = bad[0]
        return _rule("R16", title, FAIL, RECORD, "C", [NOT_A_VALID_MATING_PART],
                     f"{other.get('mpn')!r}, {side['id']} için beyan edilen "
                     f"eşleşme listesinde yok ({allowed})")
    return _rule("R16", title, PASS, RECORD, "C", [],
                 f"{len(checks)} yönde MPN beyan edilen listede")


COMPARED_FIELDS = ("pitch_mm", "positions", "conductors", "contact_face",
                   "thickness_mm", "width_mm", "margin_mm", "stiffener_face",
                   "contact_orientation", "stack_height_mm", "contact_finish",
                   "lane_count")


def r17_provenance(edge: Edge) -> Tuple[dict, List[dict]]:
    """(kural sonucu, cap listesi).

    Bir alan KARŞILAŞTIRILDIYSA provenansı da gelmek zorundadır. ``revision``
    listede çünkü revizyonsuz bir belge bir BELGE AİLESİNİ adlandırır ve
    okunan nüsha sonradan geri getirilemez. ``basis`` değeri ``derived`` /
    ``vendor_page`` ise sonuç en iyi ``SCREEN_ONLY``, ``local_policy`` ise
    ``LOCAL_POLICY``'dir - türetilmiş bir sayı imza açmaz.
    """
    title = "provenans zorunluluğu (alan başına beş alan + basis)"
    caps: List[dict] = []
    sources: List[Tuple[str, str, dict]] = []
    for side in (edge.side_a, edge.side_b):
        if side is None:
            continue
        prov = side.get("provenance") or {}
        for field in COMPARED_FIELDS:
            if side.get(field) is None:
                continue
            sources.append((str(side.get("id")), field,
                            prov.get(field) if isinstance(prov, dict) else None))
    medium_prov = (edge.medium or {}).get("provenance") or {}
    for field in COMPARED_FIELDS:
        if (edge.medium or {}).get(field) is None:
            continue
        sources.append(("medium", field,
                        medium_prov.get(field)
                        if isinstance(medium_prov, dict) else None))
    if not sources:
        return (_rule("R17", title, NO_COVERAGE, SIGNOFF, "C", [],
                      "karşılaştırılan hiçbir alan yok, dolayısıyla "
                      "provenans sorusu da yok"), caps)
    gaps: List[dict] = []
    for owner, field, source in sources:
        if not isinstance(source, dict):
            gaps.append({"owner": owner, "field": field,
                         "missing": list(PROVENANCE_FIELDS)})
            continue
        missing = [f for f in PROVENANCE_FIELDS if _text(source.get(f)) is None]
        if missing:
            gaps.append({"owner": owner, "field": field, "missing": missing})
            if missing == ["revision"]:
                caps.append({"verdict": CONFIRM, "edge_id": edge.edge_id,
                             "rule": "R17",
                             "reason": f"{owner}.{field}: provenansta "
                                       "'revision' yok - revizyonsuz bir belge "
                                       "bir belge AİLESİNİ adlandırır"})
        basis = _text(source.get("basis"))
        if basis in BASIS_SCREEN:
            caps.append({"verdict": SCREEN_ONLY, "edge_id": edge.edge_id,
                         "rule": "R17",
                         "reason": f"{owner}.{field}: basis={basis} - "
                                   "türetilmiş/satıcı-sayfası veri imza açmaz"})
        elif basis in BASIS_LOCAL:
            caps.append({"verdict": LOCAL_POLICY, "edge_id": edge.edge_id,
                         "rule": "R17",
                         "reason": f"{owner}.{field}: basis=local_policy - "
                                   "bu depoda seçilmiş, otorite değil"})
    if gaps:
        for gap in gaps:
            if gap["missing"] != ["revision"]:
                caps.append({"verdict": CONFIRM, "edge_id": edge.edge_id,
                             "rule": "R17",
                             "reason": f"{gap['owner']}.{gap['field']}: "
                                       "provenansta "
                                       f"{', '.join(gap['missing'])} yok - "
                                       "kaynaksız bir karşılaştırma GREEN "
                                       "üretemez"})
        entry = _rule("R17", title, FAIL, SIGNOFF, "C", [UNVERIFIED_ATTRIBUTE],
                      f"{len(gaps)}/{len(sources)} karşılaştırılan alanın "
                      "provenansı eksik")
        entry["gaps"] = gaps
        entry["compared_fields"] = len(sources)
        return entry, caps
    entry = _rule("R17", title, PASS, SIGNOFF, "C", [],
                  f"{len(sources)} karşılaştırılan alanın hepsi beş alanlı "
                  "provenans taşıyor")
    entry["gaps"] = []
    entry["compared_fields"] = len(sources)
    return entry, caps


def r19_plating(edge: Edge) -> dict:
    title = "kaplama uyumu"
    a, b = edge.side_a, edge.side_b
    if edge.edge_class != "mating" or a is None or b is None:
        return _rule("R19", title, NO_COVERAGE, RECORD, "C", [], "taraf yok")
    fa, fb = a.get("contact_finish"), b.get("contact_finish")
    if not _both(fa, fb):
        return _rule("R19", title, NO_COVERAGE, RECORD, "C", [],
                     "contact_finish iki tarafta birden beyan edilmemiş")
    if fa != fb:
        return _rule("R19", title, FAIL, RECORD, "C", [PLATING_MISMATCH],
                     f"{fa} <-> {fb}: farklı kaplama çifti galvanik aşınma "
                     "riskidir; karar konnektor skill'inindir")
    return _rule("R19", title, PASS, RECORD, "C", [], f"iki taraf da {fa}")


def r20_mating_cycles(edge: Edge, required: Optional[int]) -> dict:
    title = "mating cycle bütçesi"
    if required is None:
        return _rule("R20", title, NO_COVERAGE, RECORD, "C", [],
                     "--bringup-mate-cycles verilmedi: kimsenin söylemediği "
                     "bir bütçe karşılanmış olamaz")
    sides = [s for s in (edge.side_a, edge.side_b) if s is not None]
    known = [(s["id"], s["mating_cycles"]) for s in sides
             if s.get("mating_cycles") is not None]
    if not known:
        return _rule("R20", title, NO_COVERAGE, RECORD, "C", [],
                     "hiçbir tarafta datasheet_cycles beyan edilmemiş")
    short = [(name, value) for name, value in known if value < required]
    if short:
        return _rule("R20", title, FAIL, RECORD, "C", [MATING_CYCLE_BUDGET],
                     f"bring-up {required} çevrim istiyor; "
                     + ", ".join(f"{n}={v}" for n, v in short))
    return _rule("R20", title, PASS, RECORD, "C", [],
                 f"{required} çevrim bütçesi karşılanıyor")


def declared_nonmatching_rule(edge: Edge) -> Optional[dict]:
    """Taraf BAKMIŞ ve açıkça YANLIŞ olduğunu beyan etmiş.

    Gerçek vaka: ``proc-a-cv72s/pins/som_cv72s_if.json:9`` - *"gercek SoM
    pinlerine KARSILIK GELMEZ"*. Bu ``NO_COVERAGE`` değil (bakıldı),
    ``PITCH_MISMATCH`` değil (tip bilinmiyor), ``PASS`` hiç değil. Kapalı
    verdict sözlüğüne yeni değer EKLENMEZ.
    """
    declared = [s for s in (edge.side_a, edge.side_b)
                if s is not None and s.get("declared_nonmatching")]
    if not declared:
        return None
    notes = "; ".join(f"{s['id']}: {s.get('declared_nonmatching_note') or '-'}"
                      for s in declared)
    return _rule("R0", "beyan edilmiş uyuşmazlık", FAIL, SIGNOFF, "C",
                 [DECLARED_NONMATCHING_CODE], notes)


MATING_RULES = ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9", "R10",
                "R11", "R12", "R13", "R14", "R15", "R16", "R17", "R18", "R19",
                "R20")
INTERNAL_RULES = ("R10", "R11", "R12", "R13", "R17")


def evaluate_edge(edge: Edge, mate_cycles: Optional[int]) -> None:
    """Kenarı değerlendir; ``edge.status`` / ``codes`` / ``caps`` doldur.

    KENAR DURUMU KURALI:
      * ``signoff`` ya da ``screen_only`` bir kuralın FAIL'i kenarı FAIL yapar
        (``screen_only`` "PASS imzalamaz" demektir, "FAIL bir şey söylemez"
        demek DEĞİL: 30 kontaklı kablo 22 kontaklı yuvaya girmez);
      * ``record_only`` bir FAIL kenarı FAIL YAPMAZ, bir CONFIRM cap ekler;
      * hiçbir FAIL yoksa ve bir ``signoff`` kural NO_COVERAGE ise kenar
        NO_COVERAGE'dır - "bakamadık" ile "baktık ve temiz" aynı şey değildir;
      * PASS yalnız her ``signoff`` kural PASS iken.
    """
    results: List[dict] = []
    caps: List[dict] = []

    nonmatching = declared_nonmatching_rule(edge)
    if nonmatching is not None:
        results.append(nonmatching)

    if edge.edge_class == "mating":
        results.append(r1_medium_present(edge))
        results.append(r2_interface_class(edge))
        results.append(r3_pitch(edge))
        results.append(r4_positions(edge))
        results.append(r5_gender(edge))
        results.append(r6_contact_face(edge))
        results.append(r7_cable_type(edge))
        results.append(r8_tail_thickness(edge))
        results.append(r9_tail_geometry(edge))
        results.append(r10_pinmap_declared(edge))
        results.append(r11_net_identity(edge))
        results.append(r12_power_on_signal(edge))
        results.append(r13_diffpair(edge))
        results.append(r14_lane_count(edge))
        results.append(r15_stack_height(edge))
        results.append(r16_valid_mating(edge))
        prov, prov_caps = r17_provenance(edge)
        results.append(prov)
        caps.extend(prov_caps)
        results.append(r18_determinacy(edge))
        results.append(r19_plating(edge))
        results.append(r20_mating_cycles(edge, mate_cycles))
        medium = edge.medium
        if medium is not None and _text(medium.get("kind")) != DIRECT_MATE \
                and _text(medium.get("bom_line")) is None:
            # bom-lifecycle MPN seçiminin sahibidir; bu kapı BOM'u düzeltmez.
            # BOM'da olmayan bir kablo CONFIRM'dir, RED değil, + L2 kenarı.
            caps.append({"verdict": CONFIRM, "edge_id": edge.edge_id,
                         "rule": "R1", "reason":
                         "medium'un BOM satırı yok (BOM_LINE_ABSENT): kablo/"
                         "adaptör ayrı bir BOM kalemidir. Seçim bom-lifecycle'ın, "
                         "bu kapı yalnız kaydeder ve L2 kenarı açar"})
            edge.codes.append(BOM_LINE_ABSENT)
    else:
        if edge.mapping is None or _text((edge.mapping or {}).get("mapping")) \
                is None:
            results.append(r10_pinmap_declared(edge))
        else:
            if not edge.pin_to_pin:
                edge.pin_to_pin = _straight_rows(edge)
            results.append(r10_pinmap_declared(edge))
            results.append(r11_net_identity(edge))
            results.append(r12_power_on_signal(edge))
            results.append(r13_diffpair(edge))
            prov, prov_caps = r17_provenance(edge)
            results.append(prov)
            caps.extend(prov_caps)

    edge.rule_results = results
    edge.caps.extend(caps)

    # R17 KENAR DURUMUNU DÜŞÜRMEZ, İMZAYI TUTAR. Provenans eksikliği ölçülmüş
    # bir uyuşmazlık DEĞİL, bir OTORİTE eksikliğidir: karşılaştırma yapıldı,
    # ama okunan belge geri getirilemiyor. FAIL saymak kaynaksızlığı yanlış bir
    # pinle eşitlerdi; yok saymak GREEN'e giderdi. Doğru araç bir CAP'tir - ve
    # cap zinciri kapıyı GREEN'e ASLA bırakmaz (en fazla CONFIRM).
    hard_fail = [r for r in results
                 if r["status"] == FAIL and r["authority"] in (SIGNOFF, SCREEN)
                 and r["rule"] != "R17"]
    soft_fail = [r for r in results
                 if r["status"] == FAIL and r["authority"] == RECORD]
    for entry in soft_fail:
        edge.caps.append({"verdict": CONFIRM, "edge_id": edge.edge_id,
                          "rule": entry["rule"],
                          "reason": f"{entry['rule']} (record_only) FAIL: "
                                    f"{entry['detail']}"})
    signoff_gaps = [r for r in results
                    if r["status"] == NO_COVERAGE and r["authority"] == SIGNOFF
                    and r["rule"] != "R17"]

    for entry in results:
        edge.codes.extend(entry["codes"])
    edge.codes.extend(code for entry in soft_fail for code in entry["codes"])

    if hard_fail:
        edge.status = FAIL
        edge.detail = (edge.detail or "") + " | " + "; ".join(
            f"{r['rule']}:{','.join(r['codes']) or 'FAIL'}" for r in hard_fail)
    elif signoff_gaps:
        edge.status = NO_COVERAGE
        edge.detail = (edge.detail or "") + " | ölçülemedi: " + ", ".join(
            r["rule"] for r in signoff_gaps)
    else:
        edge.status = PASS


# --------------------------------------------------------------------------- #
# 5. Dışlamalar
# --------------------------------------------------------------------------- #

def parse_exclusions(raw: Sequence[str], known_ids: Sequence[str]
                     ) -> Tuple[List[dict], List[dict]]:
    """(gerekçeli, gerekçesiz).

    ``sistem-arayuz-sozlesmesi:150-158``'den miras: gerekçe taşımayan birim
    ``unjustified_exclusions``'a düşer ve verdict RED olur. Aritmetik her
    blokta korunur; değişen şey, bir birimi ``excluded``'a KİMİN koyabildiğidir.
    Gerekçesiz bir dışlama öğeyi taranan nüfustan ÇIKARMAZ.
    """
    justified: List[dict] = []
    unjustified: List[dict] = []
    # KENAR KİMLİKLERİ ':' İÇERİR (``mate:ic_1:a``), dolayısıyla ilk iki nokta
    # üzerinden bölmek ``mate:ic_1``'i "mate adlı kenar, gerekçe 'ic_1'" diye
    # okur - gerekçesiz bir dışlama sessizce GEREKÇELİ hâle gelirdi ve F4'ün
    # muhafızı bütünüyle boşa düşerdi. Bu yüzden bilinen kimliklerin en UZUN
    # öneki aranır.
    ordered = sorted(set(known_ids), key=len, reverse=True)
    for item in raw:
        text = str(item).strip()
        edge_id, reason = None, ""
        if text in ordered:
            edge_id, reason = text, ""
        else:
            for candidate in ordered:
                if text.startswith(candidate + ":"):
                    edge_id = candidate
                    reason = text[len(candidate) + 1:].strip()
                    break
        if edge_id is None:
            edge_id, _, reason = text.partition(":")
            edge_id, reason = edge_id.strip(), reason.strip()
        if not edge_id:
            raise InterconnectMatingError(
                "--exclude-link boş bir kenar kimliği taşıyor")
        if not reason:
            unjustified.append({
                "edge_id": edge_id, "issue": UNJUSTIFIED_EXCLUSION,
                "detail": "gerekçesiz dışlama: öğe taranmaya DEVAM eder ve "
                          "red rapora yazılır. excluded, 'bakılmadı'nın kibar "
                          "hâli değildir"})
            continue
        justified.append({"edge_id": edge_id, "reason": reason})
    return justified, unjustified


# --------------------------------------------------------------------------- #
# 6. Bulgular, kapsam ve verdict
# --------------------------------------------------------------------------- #

CHECK_EDGES = "her_kenarin_durumu_bir_bulguya_bagli"
CHECK_LINKAGE = "sozlesme_baglantilari_zincire_baglandi"
CHECK_MEDIUM = "her_halkanin_medium_u_var"
CHECK_MATING = "fiziksel_takilabilirlik"
CHECK_PINMAP = "pin_ortusmesi"
CHECK_DAMAGE = "guc_veri_carpismasi_yok"
CHECK_INTERNAL = "ic_gecis_eslemesi"
PROVENANCE_AXIS = "karsilastirilan_alanlarin_provenansi"  # cap ekseni, bulgu degil
CHECK_EXCLUSION = "dislamalar_gerekceli"


def _rule_status(edges: Sequence[Edge], rule_ids: Sequence[str]
                 ) -> Tuple[int, List[dict]]:
    """(kaç kenarda bu kural(lar) ölçüldü, ihlal listesi).

    ``record_only`` bir kuralın FAIL'i İHLAL SAYILMAZ ve bu bilinçlidir:
    ``authority: record_only`` "temiz bir sonuç bir şey kapatmaz, kirli bir
    sonuç da kapıyı kapatmaz - yalnız kayda geçer" demektir. Kaplama
    uyuşmazlığını (R19) ölçülmüş bir takılamazlıkla eşitlemek, kararı
    ``konnektor``'den çalmak olurdu. Görünürlüğü bir ``CONFIRM`` cap taşır,
    dolayısıyla sessizleşmez.
    """
    scanned = 0
    violations: List[dict] = []
    for edge in edges:
        picked = [r for r in edge.rule_results if r["rule"] in rule_ids]
        if not picked:
            continue
        if all(r["status"] == NO_COVERAGE for r in picked):
            continue
        scanned += 1
        failed = [r for r in picked if r["status"] == FAIL
                  and r["authority"] in (SIGNOFF, SCREEN)]
        if failed:
            violations.append({
                "edge_id": edge.edge_id, "edge_class": edge.edge_class,
                "chain_id": edge.chain_id, "contract_link": edge.contract_link,
                "codes": sorted({c for r in failed for c in r["codes"]}),
                "detail": "; ".join(f"{r['rule']}: {r['detail']}"
                                    for r in failed)})
    return scanned, violations


def build_findings(edges: Sequence[Edge], unmatched: Sequence[dict],
                   contract_links: Sequence[ContractLink],
                   unjustified: Sequence[dict]) -> List[Finding]:
    scanned_edges = [e for e in edges if not e.excluded]
    findings: List[Finding] = []

    # (0) MUHAFIZIN MUHAFIZI. Aşağıdaki bulgular kuralları GRUPLARA yönlendirir
    #     ve bir grubu güncellemeyi unutmak, o kuralın FAIL'ini rollup'a hiç
    #     sokmaz: `coverage.violations` 2 derken her bulgu PASS kalır ve kapı
    #     GREEN verir. Ölçüldü (R0 beyan-edilmiş-uyuşmazlık kuralı tam olarak
    #     bunu yaptı). Bu bulgu her kenarın durumunu doğrudan sayar, dolayısıyla
    #     yönlendirilmeyi unutulan bir kural sessiz kalamaz.
    if scanned_edges:
        findings.append(result(
            CHECK_EDGES, len(scanned_edges),
            [{"edge_id": e.edge_id, "edge_class": e.edge_class,
              "chain_id": e.chain_id, "contract_link": e.contract_link,
              "codes": sorted(set(e.codes)), "detail": e.detail}
             for e in scanned_edges if e.status == FAIL],
            f"{len(scanned_edges)} kenarın durumu doğrudan sayıldı"))
    else:
        findings.append(Finding(CHECK_EDGES, NO_COVERAGE, 0, [],
                                "taranacak kenar yok"))

    # (1) Sözleşme ekseni - zincirsiz arayüz burada ADIYLA görünür.
    bound = len(contract_links) - len(unmatched)
    if not contract_links:
        findings.append(Finding(CHECK_LINKAGE, NO_COVERAGE, 0, [],
                                "sözleşme hiçbir zorunlu bağlantı bildirmiyor"))
    else:
        findings.append(result(
            CHECK_LINKAGE, len(contract_links),
            [dict(v) for v in unmatched],
            f"{bound}/{len(contract_links)} zorunlu sözleşme bağının zinciri "
            "var"))

    # (2) Eksik halka.
    scanned, violations = _rule_status(scanned_edges, ("R1",))
    findings.append(result(CHECK_MEDIUM, scanned, violations,
                           f"{scanned} eşleşme kenarında medium sorusu "
                           "soruldu") if scanned else
                    Finding(CHECK_MEDIUM, NO_COVERAGE, 0, [],
                            "medium sorusu sorulabilecek eşleşme kenarı yok"))

    # (3) Fiziksel takılabilirlik (geometri + aile).
    scanned, violations = _rule_status(
        scanned_edges, ("R0", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9",
                        "R14", "R15", "R16", "R18", "R19", "R20"))
    findings.append(result(CHECK_MATING, scanned, violations,
                           f"{scanned} kenarda en az bir geometri/aile kuralı "
                           "ölçüldü") if scanned else
                    Finding(CHECK_MATING, NO_COVERAGE, 0, [],
                            "hiçbir kenarda geometri veya aile kuralı "
                            "ölçülemedi - iki taraflı ölçülmüş alan yok"))

    # (4) Pin örtüşmesi - İMZANIN TAŞIYICISI.
    scanned, violations = _rule_status(scanned_edges, ("R10", "R11", "R13"))
    findings.append(result(CHECK_PINMAP, scanned, violations,
                           f"{scanned} kenarda pin örtüşmesi ölçüldü")
                    if scanned else
                    Finding(CHECK_PINMAP, NO_COVERAGE, 0, [],
                            "hiçbir kenarda pin_to_pin çizelgesi ölçülemedi; "
                            "geometri eşitliği bunun yerine geçmez"))

    # (5) R12 kendi bulgusudur: sessiz HASAR, sessiz açık devreyle aynı
    #     önceliğe konmaz.
    scanned, violations = _rule_status(scanned_edges, ("R12",))
    findings.append(result(CHECK_DAMAGE, scanned, violations,
                           f"{scanned} kenarda ray/sinyal çarpışması arandı")
                    if scanned else
                    Finding(CHECK_DAMAGE, NO_COVERAGE, 0, [],
                            "pin sınıfları beyan edilmediği için ray/sinyal "
                            "çarpışması ARANAMADI - bu bir temizlik değil, bir "
                            "boşluktur"))

    # (6) İç geçişler.
    internal = [e for e in scanned_edges if e.edge_class == "internal"]
    scanned, violations = _rule_status(internal, ("R10", "R11", "R12", "R13"))
    findings.append(result(CHECK_INTERNAL, scanned, violations,
                           f"{scanned} iç kenarda eşleme ölçüldü")
                    if scanned else
                    Finding(CHECK_INTERNAL, NO_COVERAGE, 0, [],
                            f"{len(internal)} iç kenarın hiçbiri straight / "
                            "reversed / custom beyan etmemiş"))

    # (7) Provenans BİLİNÇLİ OLARAK BİR BULGU DEĞİLDİR ve rollup'a girmez;
    #     ayrı bir kapsam bloğu olarak yayımlanır (bkz. provenance_coverage).
    #     Emsal: cad_asset_qualification provenansı bir cap ekseni olarak
    #     tutar, altı kimlik kontrolünü bulgu olarak tutar. Sebep mekanik:
    #     rollup'a FAIL olarak girse kaynaksızlık ölçülmüş bir uyuşmazlıkla
    #     eşitlenir ve RED çıkar; NO_COVERAGE olarak girse "bakamadık" der
    #     halbuki bakıldı. İkisi de yanlış; doğru cevap CONFIRM ve onu cap
    #     taşır. Kapsam yine ASSERT EDİLEBİLİR, yalnız kendi bloğunda.

    # (8) Dışlamalar.
    if unjustified:
        findings.append(result(CHECK_EXCLUSION, len(unjustified),
                               [dict(v) for v in unjustified],
                               "gerekçesiz dışlama var"))
    return findings


ALL_RULE_IDS = ("R0",) + MATING_RULES


def rule_coverage_map(edges: Sequence[Edge]) -> Dict[str, dict]:
    """Kural başına ``status`` + ``scanned`` + ``violations``.

    NİÇİN BULGU BAŞINA DEĞİL, KURAL BAŞINA. Bulgular kuralları gruplara
    topluyor (``pin_ortusmesi`` R10+R11+R13'ü birlikte sayar), dolayısıyla
    ``metrics.pin_ortusmesi`` üzerine yazılmış bir R11 assertion'ı R10'un
    ihlalleri üzerinden de geçer/kalır — yani rulesının ölçmediği bir şeyi
    iddia eder. Matris satırı başına **kendi paydası** olmadan
    ``coverage_selector`` bağlanamaz ve ``BULGU-bos-kontrol-temiz-gorunur.md:126-133``
    uyarınca öyle bir A/B assertion'ı yüklenmemelidir (``COVERAGE_UNBOUND``):
    ``metrics.<check> at_most 0`` yazan bir assertion *"ihlal yok"* ile
    *"hiçbir şeye bakılmadı"*yı aynı sayıya indirger. Bu kapı tam olarak o
    tuzağı kapatmak için var; kendi matris satırlarının aynı tuzağa düşmesi
    kabul edilemez.

    ``scanned`` = bu kuralın GERÇEKTEN ölçüldüğü kenar sayısı (NO_COVERAGE
    dönen kenar sayılmaz). ``scanned == 0`` ise ``status`` ``NO_COVERAGE``'dır
    ve ona bağlı bir assertion coverage kapısında durur.
    """
    out: Dict[str, dict] = {}
    for rule_id in ALL_RULE_IDS:
        scanned = 0
        violations = 0
        record_only_violations = 0
        authority: Optional[str] = None
        for edge in edges:
            for entry in edge.rule_results:
                if entry["rule"] != rule_id:
                    continue
                authority = entry["authority"]
                if entry["status"] == NO_COVERAGE:
                    continue
                scanned += 1
                if entry["status"] == FAIL:
                    if entry["authority"] == RECORD:
                        record_only_violations += 1
                    else:
                        violations += 1
        if scanned == 0:
            status = NO_COVERAGE
        elif violations or record_only_violations:
            status = FAIL
        else:
            status = PASS
        out[rule_id] = {
            "status": status,
            "scanned": scanned,
            # `violations` yalnız imza/tarama yetkili ihlalleri sayar: bir
            # `record_only` FAIL kapıyı kapatmaz, bu yüzden onu buraya katmak
            # ona ait olmayan bir yetki verirdi. Ayrı alanda görünür kalır.
            "violations": violations,
            "record_only_violations": record_only_violations,
            "authority": authority,
        }
    return out


def build_coverage(eligible: int, scanned: int, excluded: int, violations: int,
                   reason: Optional[str], source: str) -> dict:
    """``scanned`` GERÇEKTEN İNCELENEN nüfustur, ``eligible - excluded`` DEĞİL.

    Farkı görmek için maliyeti ölçüldü: ``scanned``'i türetmek aritmetiği
    inşa yoluyla her zaman doğru yapar - ve tam bu yüzden EVRENDE OLMAYAN bir
    kenarı dışlamak sessizce ``scanned``'i bir azaltır ve muhafız hiçbir şey
    görmez. Sayılan iki nüfus ayrı ayrı ölçülünce hayalî bir dışlama
    ``scanned + excluded != eligible`` verir ve ``INVALID_COVERAGE`` (exit 2)
    çıkar. "Yanlış populasyondan gelen bir payda, gerçek bir muhafızla aynı
    yeşili verir" - burada payda ile pay ayrı ayrı sayılır ki fark görünsün.
    """
    return {"eligible": eligible, "scanned": scanned,
            "excluded": excluded, "violations": violations,
            "reason": reason, "source": source}


def strongest_cap(caps: Sequence[dict]) -> Optional[str]:
    for verdict in CAP_ORDER:
        if any(c["verdict"] == verdict for c in caps):
            return verdict
    return None


def domain_state(edges: Sequence[Edge], unmatched: Sequence[dict],
                 unbound: Sequence[dict]) -> Tuple[str, List[str]]:
    reasons: List[str] = []
    codes = {code for edge in edges for code in edge.codes}
    if DECLARED_NONMATCHING_CODE in codes:
        reasons.append("bir taraf kendi uyuşmazlığını açıkça beyan etti")
        return DECLARED_NONMATCHING, reasons
    broken = []
    if unmatched:
        broken.append(f"{len(unmatched)} zorunlu sözleşme bağının zinciri yok")
    if unbound:
        broken.append(f"{len(unbound)} zincir sözleşmeye bağlanmıyor")
    for code in (NO_MATED_PART, MISSING_TRANSITION, POSITION_COUNT_MISMATCH,
                 PITCH_MISMATCH, INTERFACE_CLASS_MISMATCH, GENDER_CONFLICT,
                 LANE_COUNT_MISMATCH, NET_NAME_MISMATCH, PIN_ORDER_MISMATCH,
                 POWER_ON_SIGNAL_PIN, DIFFPAIR_SWAP, DIFFPAIR_SPLIT):
        if code in codes:
            broken.append(code)
    if broken:
        return CHAIN_BROKEN, broken
    if BLOCKED_UNSPECIFIED in codes or BOM_LINE_ABSENT in codes:
        return MEDIUM_UNSPECIFIED, ["zincirdeki en az bir parça belirlenmemiş "
                                    "(MPN / üretici / revizyon / BOM satırı)"]
    if PINMAP_UNDECLARED in codes or MAPPING_UNDECLARED in codes:
        return PINMAP_UNVERIFIED, ["geometri ölçüldü ama pin örtüşmesi "
                                   "beyan edilmedi"]
    return CHAIN_COMPLETE, ["her zorunlu bağın zinciri var, her halkanın "
                            "medium'u var, pin örtüşmesi beyan ve doğrulandı"]


def final_verdict(findings: Sequence[Finding], coverage: dict,
                  contract_coverage: dict, caps: Sequence[dict],
                  stale_reasons: Sequence[str],
                  excluded_checks: Sequence[str]) -> Tuple[str, List[str]]:
    """Verdict'in karar verildiği TEK yer. Burada hiçbir verdict icat edilmez.

    Sıra ve niçin:
      1. ``INVALID_COVERAGE`` - bozuk aritmetik rapordaki her sayıyı geçersiz
         kılar, dolayısıyla aşağı akış hiçbir şeyi okuyamaz (exit 2).
      2. ``RED`` - ÖLÇÜLMÜŞ bir uyuşmazlık bir cap ile yumuşatılmaz.
      3. ``STALE`` - dosyalar ölçümün altından kaydı.
      4. ``NO_COVERAGE`` - hiçbir şey incelenmedi. Asla GREEN, asla cap.
      5. en güçlü cap.
      6. ``GREEN`` - yalnız her evren tarandı, hiçbir şey uyuşmadı ve hiçbir
         cap uygulanmıyorken erişilebilir.
    """
    for name, block in (("coverage", coverage),
                        ("contract_coverage", contract_coverage)):
        broken = coverage_rule_violations(block)
        if broken:
            return INVALID_COVERAGE, [
                f"{name} aritmetiği bozuk: {', '.join(sorted(set(broken)))} "
                f"({block['scanned']} + {block['excluded']} != "
                f"{block['eligible']})"]
    roll = rollup_status(findings, excluded=set(excluded_checks))
    if roll == FAIL:
        return RED, [f["check"] for f in findings if f["status"] == FAIL]
    if stale_reasons:
        return STALE, list(stale_reasons)
    if roll == NO_COVERAGE or coverage["scanned"] == 0:
        reasons = [f["check"] for f in findings if f["status"] == NO_COVERAGE]
        if coverage["scanned"] == 0:
            reasons.insert(0, "coverage.scanned = 0")
        return NO_COV, reasons
    cap = strongest_cap(caps)
    if cap is not None:
        return cap, sorted({c["reason"] for c in caps if c["verdict"] == cap})
    return GREEN, []


# --------------------------------------------------------------------------- #
# 7. L3 devri - limitin sahibi BU kapıdır, ölçümün değil
# --------------------------------------------------------------------------- #

L3_ROWS = [
    {
        "rule_id": "Fiziksel eşleşme: fiş yuvaya alet gerektirmeden girip "
                   "çıkıyor ve aktüatör tam kilitleniyor",
        "title": "mating fixture doğrulaması",
        "owner": "arayuz-eslesme-kapisi",
        "measurement_class": "mating_fixture",
        "campaign_stage": "power_safe",
        "required_compliance_class": "bench",
        "limit": {
            "units": "boolean",
            "min": 1,
            "authority": {
                "owner_skill": "arayuz-eslesme-kapisi",
                "document_id": "skills/arayuz-eslesme-kapisi/SKILL.md",
                "locator": "§4 Bilinen sınırlar",
            },
        },
        # konnektor/SKILL.md:200-203'teki MEVCUT fixture alanları. Yeni ad
        # İCAT EDİLMEZ: iki adla yaşayan bir alan, iki farklı değer taşır.
        "fixture_fields": ["actual_connector_mpn", "actual_fpc_thickness",
                           "datum_fixture_used", "insert_remove_without_force",
                           "actuator_full_lock_visible",
                           "post_mate_clearance_margin"],
    },
    {
        "rule_id": "Pin-1 köşelerinin uzaysal ilişkisi (mirroring paritesi) "
                   "kablo takılıyken doğrulandı",
        "title": "mirroring paritesi",
        "owner": "arayuz-eslesme-kapisi",
        "measurement_class": "continuity",
        "campaign_stage": "power_safe",
        "required_compliance_class": "bench",
        "limit": {
            "units": "count",
            "max": 0,
            "authority": {
                "owner_skill": "arayuz-eslesme-kapisi",
                "document_id": "skills/arayuz-eslesme-kapisi/SKILL.md",
                "locator": "§3 Nüanslar - numara ekseni yoktur",
            },
        },
        "fixture_fields": ["actual_connector_mpn", "datum_fixture_used"],
    },
]


# --------------------------------------------------------------------------- #
# 8. Rapor
# --------------------------------------------------------------------------- #

def build_report(contract_path: Path, chain_path: Path, *,
                 exclusions: Sequence[str] = (),
                 mate_cycles: Optional[int] = None,
                 prior_path: Optional[Path] = None,
                 connector_spec_path: Optional[Path] = None) -> dict:
    contract_doc = read_json(contract_path, "contract")
    chain_doc = read_json(chain_path, "chain")
    spec: Optional[Dict[str, dict]] = None
    if connector_spec_path is not None:
        spec = parse_connector_spec(
            read_json(connector_spec_path, "connector_spec"),
            connector_spec_path)

    contract_links, contract_shape = parse_contract(contract_doc, contract_path)
    chains = parse_chain(chain_doc, chain_path)

    edges, unbound, unmatched, derivation = build_universe(chains, contract_links)
    derivation["contract_shape"] = contract_shape
    derivation["contract_path"] = str(contract_path)

    by_id = {edge.edge_id: edge for edge in edges}
    justified, unjustified = parse_exclusions(exclusions, list(by_id))
    excluded_records: List[dict] = []
    unknown: List[dict] = []
    for entry in justified:
        edge = by_id.get(entry["edge_id"])
        if edge is None:
            # Evrende OLMAYAN bir kenarı dışlamak paydayı elle küçültme
            # girişimidir. Sayılır ve aritmetiği bozar -> INVALID_COVERAGE.
            unknown.append({**entry, "issue": UNKNOWN_EXCLUSION,
                            "detail": "kenar evrende yok; dışlama SAYILIR ve "
                                      "kapsam aritmetiğini bozar"})
            continue
        edge.excluded = True
        edge.exclusion_reason = entry["reason"]
        excluded_records.append(entry)

    for edge in edges:
        evaluate_edge(edge, mate_cycles)

    findings = build_findings(edges, unmatched, contract_links, unjustified)
    caps: List[dict] = [cap for edge in edges if not edge.excluded
                        for cap in edge.caps]

    spec_finding, spec_violations = spec_cross_check(
        [e for e in edges if not e.excluded], spec)
    findings.append(spec_finding)
    if spec is None:
        # OPSİYONEL, AMA SESSİZ DEĞİL. Eksik kanıt CONFIRM'dür, PASS değildir
        # (coverage-gate.md:21). `required: false` kapıyı deponun kendi
        # verisine kapatmamak içindir - konnektör niteliklerini çapraz
        # doğrulamadan GEÇMİŞ saymak için değil.
        caps.append({"verdict": CONFIRM, "edge_id": None,
                     "rule": "connector_spec",
                     "reason": f"{CONNECTOR_SPEC_ABSENT}: connector_spec.json "
                               "verilmedi; konnektör nitelikleri yalnız zincir "
                               "kaydından okundu ve hiçbir ikinci belgeyle "
                               "karşılaştırılmadı"})
    for entry in unbound:
        findings.append(Finding("zincir_sozlesmeye_bagli", FAIL, 1,
                                [dict(entry)],
                                "contract_ref çözülmüyor: sözleşmeye "
                                "bağlanmamış bir zincir payda olamaz"))

    scanned_edges = [e for e in edges if not e.excluded]
    violations = sum(1 for e in scanned_edges if e.status == FAIL)

    provenance_gaps: List[dict] = []
    compared_fields = 0
    for edge in scanned_edges:
        for entry in edge.rule_results:
            if entry["rule"] != "R17":
                continue
            compared_fields += entry.get("compared_fields", 0)
            for gap in entry.get("gaps") or []:
                provenance_gaps.append({"edge_id": edge.edge_id, **gap})
    provenance_coverage = build_coverage(
        compared_fields, compared_fields, 0, len(provenance_gaps),
        None if compared_fields else
        "hiçbir alan iki taraflı ölçülmedi, dolayısıyla provenans sorusu da yok",
        "kenarlarda KARŞILAŞTIRILAN alanlar (ölçülmemiş alan provenans "
        "istemez; istenirse her boş alan bir ihlal olur ve sayı anlamını "
        "yitirir)")
    excluded_count = len(excluded_records) + len(unknown)
    coverage = build_coverage(
        len(edges), len(scanned_edges), excluded_count, violations,
        None if scanned_edges else
        "hiçbir kenar taranmadı: sözleşme zorunlu bağlantı bildirmiyor ya da "
        "her kenar dışlandı",
        "interface_contract.json zorunlu bağlantı kümesi (BEYAN EDİLEN ZİNCİR "
        "LİSTESİ DEĞİL)")
    contract_coverage = build_coverage(
        len(contract_links), len(contract_links), 0, len(unmatched),
        None if contract_links else "sözleşme zorunlu bağlantı bildirmiyor",
        "interface_contract.json arayüz bölümleri")

    stale_reasons: List[str] = []
    stale_block: dict = {"prior": None, "changed": []}
    inputs = {"contract": _artifact(contract_path),
              "chain": _artifact(chain_path)}
    if connector_spec_path is not None:
        inputs["connector_spec"] = _artifact(connector_spec_path)
    if prior_path is not None:
        prior = read_json(prior_path, "prior")
        stale_block["prior"] = str(prior_path)
        prior_inputs = prior.get("inputs") or {}
        for name, current in inputs.items():
            before = (prior_inputs.get(name) or {}).get("sha256")
            if before is not None and before != current["sha256"]:
                stale_block["changed"].append(
                    {"input": name, "before": before, "after": current["sha256"]})
                stale_reasons.append(
                    f"{name} kapı koştuktan sonra değişti: ölçüm artık var "
                    "olmayan bir durum hakkındadır")

    excluded_checks = {CHECK_EXCLUSION} if not unjustified else set()
    excluded_check_records: List[dict] = []
    if spec is None:
        # `rollup_status(excluded=...)` KASITLI ve KAYITLI kapsam-dışı bırakma
        # içindir; gerekçe rapora yazılır. Niçin rollup'a NO_COVERAGE olarak
        # bırakmıyorum: kenar evreni TARANDI, eksik olan tek şey ikinci bir
        # belgeyle çapraz doğrulama. Bütün kapıyı NO_COVERAGE yapmak
        # "hiçbir şeye bakılmadı" der ve bakıldı. Doğru cevap CONFIRM'dür ve
        # onu yukarıdaki cap taşır. Bir ÇELİŞEN connector_spec ise FAIL'dir ve
        # `rollup_status` excluded bir FAIL'i ATLAMAZ - ölçülmüş bir uyuşmazlık
        # sonradan kapsam dışına alınamaz.
        excluded_checks.add(spec_finding["check"])
        excluded_check_records.append({
            "check": spec_finding["check"],
            "reason": f"{CONNECTOR_SPEC_ABSENT}: --connector-spec verilmedi. "
                      "Kenar evreni tarandı; kapsam dışı olan yalnız ikinci "
                      "belgeyle çapraz doğrulamadır ve karşılığı CONFIRM "
                      "cap'idir, GREEN değil"})
    status, reasons = final_verdict(findings, coverage, contract_coverage, caps,
                                   stale_reasons, sorted(excluded_checks))
    state, state_reasons = domain_state(scanned_edges, unmatched, unbound)

    all_rules = [dict(r, edge_id=edge.edge_id)
                 for edge in edges for r in edge.rule_results]
    reason_codes = sorted({code for edge in edges for code in edge.codes}
                          | {v["issue"] for v in unjustified}
                          | {v["issue"] for v in unknown}
                          | {v["issue"] for v in unbound}
                          | {v["issue"] for v in unmatched}
                          | {code for v in spec_violations
                             for code in v.get("codes", ())}
                          | ({CONNECTOR_SPEC_ABSENT} if spec is None else set()))

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "gate": {"verdict": status,
                 "verdict_reason": "; ".join(reasons) or None,
                 "reasons": reasons},
        "domain_state": state,
        "domain_state_reasons": state_reasons,
        "phase": "faz1_konnektor_sonrasi_sematik_oncesi",
        "signoff": False,
        "writes_board": False,
        "assurance_loop": ["L1", "L2"],
        "reason_codes": reason_codes,
        "coverage": coverage,
        "contract_coverage": contract_coverage,
        "provenance_coverage": provenance_coverage,
        "provenance_gaps": provenance_gaps,
        "eligible_derivation": derivation,
        "edges": [edge.to_dict() for edge in edges],
        "rules": all_rules,
        "rule_coverage": rule_coverage_map(scanned_edges),
        "connector_spec_violations": spec_violations,
        "findings": findings,
        "summary": summarise(findings),
        "metrics": {f["check"]: (None if f["status"] == NO_COVERAGE
                                 else len(f["violations"]))
                    for f in findings},
        "by_check": by_check_map(findings),
        "excluded_checks": excluded_check_records,
        "excluded_edges": excluded_records,
        "unjustified_exclusions": unjustified,
        "unknown_exclusions": unknown,
        "unbound_chains": unbound,
        "unmatched_contract_links": unmatched,
        "caps": caps,
        "l3_handoff": copy.deepcopy(L3_ROWS),
        "inputs": inputs,
        "stale": stale_block,
        "independence": {
            "denominator_from": "interface_contract.json (zorunlu bağlantı "
                                "kümesi)",
            "numerator_from": "interconnect_chain.json (bulunan kayıtlar)",
            "note": "Payda ile pay AYRI dosyalardan gelir. Aynı dosyadan "
                    "gelseydi kapı kendi beyanını kendi beyanıyla "
                    "karşılaştırır ve bir tautolojiye dönerdi: iki halka "
                    "beyan edilip üçüncüsü unutulduğunda eligible=2, "
                    "scanned=2, violations=0 -> GREEN olurdu ve eksik halka "
                    "evrene hiç girmezdi",
            "chain_may_not_state_the_denominator":
                "zincir dosyası eligible / coverage / edge_count alanlarından "
                f"birini taşırsa koşu reddedilir ({ELIGIBLE_FROM_CHAIN_REFUSED})",
            "limit_ownership": "L3 satırlarının limiti BU skill'e aittir, "
                               "ölçümü l3-bringup-karakterizasyon'a. Ters "
                               "atama SELF_AUTHORED_LIMIT ile reddedilir",
        },
        "params": {"exclude_link": list(exclusions),
                   "bringup_mate_cycles": mate_cycles,
                   "prior": str(prior_path) if prior_path else None},
        "limits": LIMITS,
    }
    return report


def _diagnostics(report: dict) -> None:
    marks = {PASS: "ok  ", FAIL: "RED ", NO_COVERAGE: "?   "}
    diag("")
    diag(f"interconnect_mating: {len(report['edges'])} kenar")
    for edge in report["edges"]:
        flag = "X" if edge["excluded"] else " "
        diag(f" {flag}{marks[edge['status']]} {edge['edge_id']:44s} "
             f"[{edge['edge_class']}] {', '.join(edge['reason_codes']) or '-'}")
    cov = report["coverage"]
    diag(f"  kenar kapsamı : eligible={cov['eligible']} "
         f"scanned={cov['scanned']} excluded={cov['excluded']} "
         f"violations={cov['violations']}")
    ccov = report["contract_coverage"]
    diag(f"  sözleşme      : eligible={ccov['eligible']} "
         f"scanned={ccov['scanned']} violations={ccov['violations']}")
    for finding in report["findings"]:
        diag(f"  {marks[finding['status']]} {finding['check']}: "
             f"{finding['detail']}")
    for cap in report["caps"][:8]:
        diag(f"  cap {cap['verdict']}: {cap['reason']}")
    diag(f"  domain_state : {report['domain_state']}")
    diag(f"  gate.verdict : {report['status']}  (BAĞLAYICI olan budur)")


# --------------------------------------------------------------------------- #
# 8b. Makbuz — `gate-receipt/v1` FRAGMENT'i
# --------------------------------------------------------------------------- #

RECEIPT_SCHEMA_VERSION = "gate-receipt/v1"


def build_receipt(report: dict, argv: Sequence[str], *, start_utc: str,
                  end_utc: str, exit_code: int) -> dict:
    """Bu koşu için bir ``gate-receipt/v1`` belgesi.

    HANGİ GATE-RECEIPT SÖZLEŞMESİ İZLENİYOR VE NİÇİN
    ------------------------------------------------
    Depoda **iki** gate-receipt sözleşmesi var ve **çelişiyorlar** (ölçüldü
    2026-08-10): `skills/pcb-orchestrator/references/gate_receipt.schema.json`
    `metric_semantics.<id>.metric_definition` istiyor ve
    `additionalProperties: false`; `tools/scripts/gate_receipt.py` ise
    `definition` + `sampling_step` istiyor ve bunlar olmadan reddediyor.
    İki alan kümesi tek belgede sağlanamaz.

    Bu makbuz **DOĞRULAYICIYI** izler — çünkü doğrulayıcının reddettiği bir
    makbuz hiçbir kapıyı açamaz. Aynı seçimi `ecad-varlik-kalifikasyonu` da
    yaptı; bu kapı **çelişkiyi çözmez, kaydeder**.

    Bilinçli sonuçlar:

      * ``parsed_metrics`` ile ``coverage.per_metric`` **aynı anahtar kümesini**
        taşır: ``validate_coverage`` birebir eşleşme ister. Ama **değer
        şekilleri farklıdır** ve bu ölçülerek bulundu: ``parsed_metrics`` leaf'i
        bir **sayı**, ``coverage.per_metric`` leaf'i tam bir
        ``eligible/scanned/excluded/violations`` **nesnesidir** — sayı yazmak
        doğrulayıcıda `INVALID_COVERAGE` üretir (*"coverage counts must be an
        object"*).
      * her leaf bir **tamsayı**dır. ``null`` leaf `INVALID_METRIC`'tir; ve
        ölçülmemiş bir metriğe ``0`` yazmak 0 eşiğiyle karşılaştırıldığında
        **boş bir PASS** üretirdi. Bu yüzden ``NO_COVERAGE`` bulguları raporda
        kalır, **makbuza girmez**.
      * ``blockers`` `{code, message}` **nesneleridir**, string değil, ve liste
        her zaman vardır: `null` bir TypeError'a yol açıyor.
      * ``rules`` **otoriter kaynak kaydıdır** — bu kapı için sözleşme dosyası
        *"uygulanan kural setinin kendisi"*dir, çünkü paydayı o tanımlar.
      * yollar **mutlaktır**: ``resolve_path`` göreli yolu makbuzun dizinine
        göre çözer.
    """
    from datetime import datetime, timedelta

    rows = {f["check"]: len(f["violations"]) for f in report["findings"]
            if f["status"] != NO_COVERAGE}
    # Her metriğin KENDİ kapsam bloğu. Bulgu başına dışlama yoktur (dışlama
    # kenar düzeyinde olur), dolayısıyla eligible == scanned ve aritmetik
    # inşa yoluyla tutar; yine de `coverage_rule_violations` ile sınanır.
    per_metric = {
        f["check"]: {"eligible": f["scanned"], "scanned": f["scanned"],
                     "excluded": 0, "violations": len(f["violations"])}
        for f in report["findings"] if f["check"] in rows
    }
    for name, block in per_metric.items():
        broken = coverage_rule_violations(block)
        if broken:  # pragma: no cover - inşa yoluyla tutar
            raise InterconnectMatingError(
                f"makbuz: per_metric[{name}] kapsam kuralını bozuyor: {broken}")
    semantics = {
        name: {
            "definition": f"{name}: ihlal veren kenar/birim SAYISI "
                          "(arayuz-eslesme-kapisi R1-R20)",
            "metric_definition": f"{name} ihlal sayısı",
            "units": "count",
            "distance_semantics": "not_applicable",
            "region": "interface_contract'ın gerektirdiği ara-bağlantı kenar "
                      "evreni",
            "sampling_step": ("not_applicable: kesikli kenar sayımı, örnekleme "
                              "adımı yok"),
            "geometry_model": "not_applicable",
        }
        for name in rows
    }

    verdict = report["gate"]["verdict"]
    blockers = [{"code": f"CHECK_{name.upper()}", "message": name}
                for name in report["gate"]["reasons"]
                if isinstance(name, str) and name in rows]
    blockers += [{"code": "VERDICT_REASON", "message": str(reason)}
                 for reason in report["gate"]["reasons"]
                 if not (isinstance(reason, str) and reason in rows)]
    blockers += [{"code": f"CAP_{cap['verdict']}", "message": cap["reason"]}
                 for cap in report["caps"]]

    coverage = {k: report["coverage"][k]
                for k in ("eligible", "scanned", "excluded", "violations")}
    coverage["per_metric"] = per_metric
    if coverage["scanned"] == 0:
        coverage["reason"] = report["coverage"].get("reason") or \
            "hiçbir ara-bağlantı kenarı taranamadı"

    retain = (["receipt", "hashes", "metrics", "reproducibility_command"]
              if verdict == GREEN else
              ["receipt", "child_receipts", "logs", "scratch"])
    expires = end_utc
    try:
        parsed_end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
        expires = (parsed_end + timedelta(days=365)).isoformat() \
            .replace("+00:00", "Z")
    except ValueError:
        pass

    tool_path = Path(__file__).resolve()
    inputs = [{"role": role,
               "path": str(Path(item["path"]).resolve()),
               "sha256": item["sha256"]}
              for role, item in sorted(report["inputs"].items())
              if item.get("path") and item.get("sha256")]
    if not inputs:
        inputs = [{"role": "none", "path": "<hash-bağlı girdi yok>",
                   "sha256": "0" * 64}]
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "gate_id": "interconnect-mating",
        "gate_kind": "L1",
        "verdict": verdict,
        "status": verdict,
        "tool": {"name": "interconnect-mating", "version": SCHEMA_VERSION,
                 "path": str(tool_path), "sha256": sha256_file(tool_path)},
        "argv": list(argv) or [TOOL_NAME],
        "start_utc": start_utc,
        "end_utc": end_utc,
        "started_utc": start_utc,
        "finished_utc": end_utc,
        "exit_code": exit_code,
        "inputs": inputs,
        "parsed_metrics": dict(rows),
        "metric_semantics": semantics,
        "coverage": coverage,
        "blockers": blockers,
        "child_receipts": [],
        "retention": {"policy": "audit-window-365d", "expires_utc": expires,
                      "retain": retain},
    }
    contract = report["inputs"].get("contract") or {}
    if contract.get("path") and contract.get("sha256"):
        # Bu kapı için sözleşme dosyası KURAL SETİNİN KENDİSİDİR: paydayı o
        # tanımlar. Başka bir yeri göstermek dekorasyon olurdu.
        receipt["rules"] = {"path": str(Path(contract["path"]).resolve()),
                            "sha256": contract["sha256"]}
    return receipt


# --------------------------------------------------------------------------- #
# 9. CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract", type=Path,
                        help="interface_contract.json - PAYDANIN kaynağı. "
                             "Zorunlu: evren beyan edilmiş zincirden türemez")
    parser.add_argument("--chain", type=Path,
                        help="interconnect_chain.json (interconnect-chain/v1)")
    parser.add_argument("--exclude-link", action="append", default=[],
                        metavar="ID:GEREKÇE",
                        help="bir kenarı AÇIK gerekçeyle kapsam dışına al. "
                             "Gerekçesiz olan RED üretir")
    parser.add_argument("--connector-spec", type=Path, dest="connector_spec",
                        help="connector_spec.json (connector-spec/v1) - "
                             "konnektör niteliklerinin İKİNCİ belgesi. "
                             "Opsiyonel ama SESSİZ DEĞİL: verilmezse koşu bir "
                             f"{CONNECTOR_SPEC_ABSENT} cap'i taşır ve GREEN "
                             "ÜRETEMEZ")
    parser.add_argument("--bringup-mate-cycles", type=int, dest="mate_cycles",
                        help="bring-up planının istediği takıp-çıkarma "
                             "sayısı (R20). VARSAYILANI YOKTUR")
    parser.add_argument("--prior", type=Path,
                        help="önceki interconnect_mating_report.json - "
                             "girdiler değişmişse STALE")
    parser.add_argument("--input-json", type=Path, dest="input_json",
                        help="tüm argümanları bir JSON nesnesinden oku "
                             "(kanonik CLI sözleşmesi)")
    parser.add_argument("--json", type=Path,
                        help="raporu buraya yaz (HER ZAMAN yazar)")
    parser.add_argument("--receipt", type=Path,
                        help="interconnect_gate_receipt.json - gate-receipt/v1 "
                             "FRAGMENT'i. Kapıyı kapatan şey bu dosya DEĞİL, "
                             "onu doğrulayan tools/scripts/gate_receipt.py'dir")
    parser.add_argument("--now", type=str,
                        help="ISO-8601 zaman damgası (makbuz tekrar "
                             "üretilebilirliği)")
    parser.add_argument("--self-test", action="store_true")
    return parser


def _now_iso(value: Optional[str] = None) -> str:
    if value:
        return value
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds") \
        .replace("+00:00", "Z")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()

    contract, chain = args.contract, args.chain
    exclusions = list(args.exclude_link)
    mate_cycles, prior = args.mate_cycles, args.prior
    connector_spec = args.connector_spec
    if args.input_json is not None:
        payload = read_json(args.input_json, "input-json")
        contract = Path(payload["contract"]) if payload.get("contract") \
            else contract
        chain = Path(payload["chain"]) if payload.get("chain") else chain
        exclusions += list(payload.get("exclude_link") or [])
        mate_cycles = payload.get("bringup_mate_cycles", mate_cycles)
        prior = Path(payload["prior"]) if payload.get("prior") else prior
        connector_spec = Path(payload["connector_spec"]) \
            if payload.get("connector_spec") else connector_spec

    missing = [name for name, value in (("--contract", contract),
                                        ("--chain", chain)) if value is None]
    if missing:
        raise InterconnectMatingError(
            f"{', '.join(missing)} zorunlu. İKİSİ DE gerekiyor: payda "
            "sözleşmeden, pay zincirden gelir. Tek dosyayla koşmak, kapının "
            "kendi beyanını kendi beyanıyla karşılaştırması olurdu")

    start_utc = _now_iso(args.now)
    report = build_report(contract, chain, exclusions=exclusions,
                          mate_cycles=mate_cycles, prior_path=prior,
                          connector_spec_path=connector_spec)
    _diagnostics(report)
    write_report(report, args.json)
    exit_code = status_exit_code(report["status"])
    if args.receipt is not None:
        write_report(build_receipt(report, sys.argv[1:] or [TOOL_NAME],
                                   start_utc=start_utc,
                                   end_utc=_now_iso(args.now),
                                   exit_code=exit_code),
                     args.receipt)
    emit(report)
    return exit_code


# --------------------------------------------------------------------------- #
# 10. Self-test - her assertion, izin veren bir sürümün PASS diyeceği bir vaka
# --------------------------------------------------------------------------- #

def _source(basis: str = "datasheet") -> dict:
    return {"document_id": "FIXTURE-DS", "revision": "R1",
            "locator": "§3.2", "accessed": "2026-08-12",
            "snapshot_sha256": "0" * 64, "basis": basis}


def _prov(*fields: str, basis: str = "datasheet") -> dict:
    return {field: _source(basis) for field in fields}


FIXTURE_FACE_FIELDS = ("pitch_mm", "positions", "contact_face",
                       "contact_finish", "lane_count")
FIXTURE_MEDIUM_FIELDS = ("pitch_mm", "conductors", "thickness_mm", "width_mm",
                         "margin_mm", "stiffener_face", "contact_orientation",
                         "contact_finish")


def _fixture_pins(reverse: bool = False) -> List[dict]:
    pins = [
        {"pin": "1", "net": "GND", "class": "GND"},
        {"pin": "2", "net": "D0_P", "class": "DIFF_P", "pair": "D0"},
        {"pin": "3", "net": "D0_N", "class": "DIFF_N", "pair": "D0"},
        {"pin": "4", "net": "SCL", "class": "SIGNAL"},
        {"pin": "5", "net": "VDD_3V3", "class": "POWER"},
    ]
    if reverse:
        # SCL (pin 4, SIGNAL) ile VDD_3V3 (pin 5, POWER) yer değiştirir.
        # Pitch aynı, pozisyon sayısı aynı, NET KÜMESİ aynı - yalnız SIRA
        # yanlış. Bu tam olarak R12'nin öldürücü vakasıdır: 3.3 V rayı bir
        # veri pinine biner ve sonuç "çalışmaz" değil, KALICI HASAR'dır.
        nets = [(p["net"], p["class"], p.get("pair")) for p in pins]
        nets[3], nets[4] = nets[4], nets[3]
        for pin, (net, cls, pair) in zip(pins, nets):
            pin["net"], pin["class"] = net, cls
            if pair:
                pin["pair"] = pair
            else:
                pin.pop("pair", None)
    return pins


def _fixture_face(face_id: str, *, gender: str, contact_face: str,
                  reverse: bool = False, pitch: float = 0.5,
                  positions: int = 5, basis: str = "datasheet") -> dict:
    return {
        "face_id": face_id, "refdes": face_id,
        "interface_type": "FIXTURE-FFC-5P",
        "interface_class": "FFC_ZIF", "gender": gender,
        "mpn": f"FIXTURE-{face_id}", "manufacturer": "FIXTURE", "revision": "A",
        "pitch_mm": pitch, "positions": positions,
        "contact_face": contact_face, "pin1_reference": "datum A",
        "numbering_direction": "LEFT_TO_RIGHT",
        "accepts_thickness_mm": {"nom": 0.3, "tol": 0.05},
        "contact_finish": "Au", "mating_cycles": 20,
        "lane_count": 1, "valid_mating_mpns": ["FIXTURE-CABLE"],
        "pin_map": _fixture_pins(reverse),
        "provenance": _prov(*FIXTURE_FACE_FIELDS, basis=basis),
    }


def _fixture_chain(*, medium: Any = "default", drop_revision: bool = False,
                   pitch_b: float = 0.5, reverse_b: bool = False,
                   basis: str = "datasheet",
                   mapping: Optional[str] = "straight") -> dict:
    rows = [{"from_pin": str(i), "to_pin": str(i)} for i in range(1, 6)]
    if medium == "default":
        medium = {
            "kind": "FFC_CABLE", "mpn": "FIXTURE-CABLE",
            "manufacturer": "FIXTURE", "revision": "A",
            "pitch_mm": 0.5, "conductors": 5,
            "contact_orientation": "SAME_SIDE",
            "vendor_contact_letter": "A",
            "thickness_mm": 0.3, "width_mm": 4.0, "margin_mm": 0.5,
            "stiffener_face": ["BOTTOM", "BOTTOM"],
            "shielded": False, "contact_finish": "Au", "mating_cycles": 20,
            "mapping": mapping,
            "pin_to_pin": list(rows), "bom_line": "W99",
            "provenance": _prov(*FIXTURE_MEDIUM_FIELDS, basis=basis),
        }
    if isinstance(medium, dict) and drop_revision:
        prov = dict(medium["provenance"])
        pitch = dict(prov["pitch_mm"])
        pitch["revision"] = None
        prov["pitch_mm"] = pitch
        medium = dict(medium, provenance=prov)
    face_a = _fixture_face("J1", gender="RECEPTACLE", contact_face="TOP",
                           basis=basis)
    face_b = _fixture_face("J2", gender="RECEPTACLE", contact_face="TOP",
                           reverse=reverse_b, pitch=pitch_b, basis=basis)
    return {
        "schema_version": CHAIN_SCHEMA_VERSION,
        "chains": [{
            "chain_id": "fixture_chain",
            "contract_ref": "fixture.json#/if_a",
            "nodes": [
                {"node_id": "board_a", "kind": "BOARD", "faces": [face_a]},
                {"node_id": "board_b", "kind": "BOARD", "faces": [face_b]},
            ],
            "interconnects": [{
                "link_id": "ic_1",
                "endpoints": [{"node_id": "board_a", "face_id": "J1"},
                              {"node_id": "board_b", "face_id": "J2"}],
                "medium": medium,
                "pin_to_pin": list(rows),
                "provenance": _prov("pin_to_pin", basis=basis),
            }],
        }],
    }


def _fixture_contract(links: int = 1) -> dict:
    names = ["if_a", "if_b", "if_c"][:links]
    return {"$schema": "pcb-agent/interface_contract/v1",
            **{name: {"ref": f"J{index + 1}", "name": name}
               for index, name in enumerate(names)}}


def _fixture_connector_spec(pitch: float = 0.5, positions: int = 5) -> dict:
    """İKİNCİ belge. Zincirle AYNI büyüklükleri iddia eder; ayrılırlarsa FAIL."""
    return {
        "schema_version": CONNECTOR_SPEC_VERSION,
        "connectors": [
            {"refdes": refdes, "mpn": f"FIXTURE-{refdes}",
             "pitch_mm": pitch, "positions": positions,
             "contact_finish": "Au", "mating": {"datasheet_cycles": 20}}
            for refdes in ("J1", "J2")
        ],
    }


def _run_fixture(chain: dict, contract: Optional[dict] = None,
                 spec: Any = "default", **kwargs) -> dict:
    """Fixture'ı GERÇEK CLI yolundan koştur: dosyaya yaz, build_report çağır.

    Bellekte bir sözlük değerlendirmek, sürüm sabitleme ve hash bağı gibi
    dosya-düzeyi muhafızları atlar; bu yüzden self-test de diskten okur.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        contract_path = root / "interface_contract.json"
        chain_path = root / "interconnect_chain.json"
        contract_path.write_text(
            json.dumps(contract or _fixture_contract(), ensure_ascii=False),
            encoding="utf-8")
        chain_path.write_text(json.dumps(chain, ensure_ascii=False),
                             encoding="utf-8")
        spec_path: Optional[Path] = None
        if spec == "default":
            spec = _fixture_connector_spec()
        if spec is not None:
            spec_path = root / "connector_spec.json"
            spec_path.write_text(json.dumps(spec, ensure_ascii=False),
                                 encoding="utf-8")
        return build_report(contract_path, chain_path,
                            connector_spec_path=spec_path, **kwargs)


def _codes(report: dict) -> set:
    return set(report["reason_codes"])


def _rule_of(report: dict, rule: str, edge_prefix: str = "mate:") -> List[dict]:
    return [r for r in report["rules"]
            if r["rule"] == rule and str(r["edge_id"]).startswith(edge_prefix)]


def _self_test() -> int:
    # ---- NEGATİF TARAF: bozulmamış fixture GREEN vermeli ------------------ #
    # Yoksa her şeye RED diyen bir kapı da bu testi geçer.
    good = _run_fixture(_fixture_chain())
    assert good["status"] == GREEN, (good["status"], good["gate"]["reasons"],
                                    [c["reason"] for c in good["caps"]])
    assert good["domain_state"] == CHAIN_COMPLETE, good["domain_state"]
    assert good["coverage"] == {
        "eligible": 3, "scanned": 3, "excluded": 0, "violations": 0,
        "reason": None,
        "source": good["coverage"]["source"]}, good["coverage"]
    assert not coverage_rule_violations(good["coverage"])
    assert status_exit_code(good["status"]) == 0

    # ---- F1: zincirden bir halkayı sil (medium -> null) ------------------- #
    f1 = _run_fixture(_fixture_chain(medium=None))
    assert f1["status"] == RED, f1["status"]
    assert NO_MATED_PART in _codes(f1), f1["reason_codes"]
    # Halka ADIYLA raporda: hem kenar kimliğinde hem bulgu ihlalinde.
    named = [e for e in f1["edges"] if NO_MATED_PART in e["reason_codes"]]
    assert named and named[0]["edge_id"] == "mate:ic_1", named
    assert any("ic_1" in json.dumps(f["violations"], ensure_ascii=False)
               for f in f1["findings"] if f["check"] == CHECK_MEDIUM), f1

    # ---- F2: halkayı sil VE evreni küçültmeye çalış ----------------------- #
    # F1 tek başına yetersizdir: yakaladığı şey BEYAN LİSTESİNDEKİ bir
    # boşluktur, gerçeklikteki değil. F2 paydanın nereden geldiğini sınar.
    # (a) zincir dosyası paydayı BEYAN edemez.
    shrunk = _fixture_chain(medium=None)
    shrunk["chains"][0]["eligible"] = 2
    try:
        _run_fixture(shrunk)
    except InterconnectMatingError as exc:
        assert ELIGIBLE_FROM_CHAIN_REFUSED in str(exc), str(exc)
    else:                                                # pragma: no cover
        raise AssertionError("zincir paydayı beyan edebildi")
    # (b) halkayı tamamen SİLMEK bile paydayı küçültmez: sözleşme onu istiyor.
    empty = _fixture_chain()
    empty["chains"][0]["interconnects"] = []
    f2 = _run_fixture(empty)
    assert f2["coverage"]["eligible"] >= 1, f2["coverage"]
    assert f2["status"] == RED, f2["status"]
    assert f2["status"] != NO_COV and f2["status"] != GREEN
    # (c) zinciri tümden kaldırmak: arayüz hâlâ evrende, asgari kenarıyla.
    f2c = _run_fixture({"schema_version": CHAIN_SCHEMA_VERSION, "chains": []},
                       contract=_fixture_contract(2))
    assert f2c["coverage"]["eligible"] == 2, f2c["coverage"]
    assert f2c["status"] == RED, f2c["status"]
    assert MISSING_TRANSITION in _codes(f2c)

    # ---- F3: hiç zincir + hiç zorunlu bağ -> NO_COVERAGE ------------------ #
    # scanned=0 kuralı. Sözleşme bir arayüz bildirmiyorsa taranacak kenar da
    # yoktur ve bu asla GREEN değildir.
    f3 = _run_fixture({"schema_version": CHAIN_SCHEMA_VERSION, "chains": []},
                      contract={"schema_version": "interface-contract/v1",
                                "interfaces": [], "coverage": {}, "status": "TBD"})
    assert f3["status"] == NO_COV, f3["status"]
    assert f3["coverage"]["scanned"] == 0
    assert f3["coverage"]["reason"], f3["coverage"]
    assert status_exit_code(f3["status"]) == 1

    # ---- F4: gerekçesiz excluded -> RED ---------------------------------- #
    f4 = _run_fixture(_fixture_chain(), exclusions=["mate:ic_1:a"])
    assert f4["status"] == RED, f4["status"]
    assert UNJUSTIFIED_EXCLUSION in _codes(f4)
    assert f4["coverage"]["excluded"] == 0, (
        "gerekçesiz dışlama öğeyi taranan nüfustan ÇIKARMAZ")
    # Gerekçeli olan çıkarır ve aritmetik korunur.
    ok = _run_fixture(_fixture_chain(),
                      exclusions=["int:ic_1:medium:kablo ayrı zincirde ölçüldü"])
    assert ok["coverage"]["excluded"] == 1, ok["coverage"]
    assert not coverage_rule_violations(ok["coverage"])

    # ---- F5: pitch bozuldu, POZİSYON SABİT ------------------------------- #
    # R4 aynı koşuda PASS KALMALI. PASS kalması bir kusur değil, bu kapının
    # varlık sebebinin KANITIDIR.
    f5 = _run_fixture(_fixture_chain(pitch_b=0.4))
    assert f5["status"] == RED, f5["status"]
    assert PITCH_MISMATCH in _codes(f5)
    r4 = _rule_of(f5, "R4")
    assert r4 and all(r["status"] == PASS for r in r4), r4
    assert all(r["authority"] == SCREEN for r in r4), r4

    # ---- F6: pin sırası permüte, PITCH + SAYI SABİT ---------------------- #
    f6 = _run_fixture(_fixture_chain(reverse_b=True))
    assert f6["status"] == RED, f6["status"]
    assert PIN_ORDER_MISMATCH in _codes(f6), f6["reason_codes"]
    assert NET_NAME_MISMATCH in _codes(f6)
    # Aynı permütasyon 3V3'ü bir D-PHY lane'ine bindirir: R12 ÖLDÜRÜCÜ.
    assert POWER_ON_SIGNAL_PIN in _codes(f6), f6["reason_codes"]
    for rule in ("R3", "R4"):
        picked = _rule_of(f6, rule)
        assert picked and all(r["status"] == PASS for r in picked), (rule, picked)

    # ---- F7: türetilmiş veri -> asla GREEN ------------------------------- #
    f7 = _run_fixture(_fixture_chain(basis="derived"))
    assert f7["status"] in (SCREEN_ONLY, LOCAL_POLICY), f7["status"]
    assert f7["status"] != GREEN
    f7b = _run_fixture(_fixture_chain(basis="local_policy"))
    assert f7b["status"] == LOCAL_POLICY, f7b["status"]

    # ---- F8: provenanstan revision düşür -> en fazla CONFIRM ------------- #
    f8 = _run_fixture(_fixture_chain(drop_revision=True))
    assert f8["status"] == CONFIRM, (f8["status"],
                                     [c["reason"] for c in f8["caps"]])

    # ---- F9: kapı koştuktan sonra kaydı değiştir -> STALE ---------------- #
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        contract_path = root / "interface_contract.json"
        chain_path = root / "interconnect_chain.json"
        contract_path.write_text(json.dumps(_fixture_contract()),
                                 encoding="utf-8")
        chain_path.write_text(json.dumps(_fixture_chain()), encoding="utf-8")
        first = build_report(contract_path, chain_path)
        prior_path = root / "prior.json"
        write_report(first, prior_path)
        mutated = _fixture_chain()
        mutated["chains"][0]["note"] = "segment kaydı kapıdan SONRA değişti"
        chain_path.write_text(json.dumps(mutated), encoding="utf-8")
        f9 = build_report(contract_path, chain_path, prior_path=prior_path)
        assert f9["status"] == STALE, f9["status"]
        assert f9["stale"]["changed"], f9["stale"]

    # ---- F10: excluded'ı elle 1 artır -> INVALID_COVERAGE (exit 2) ------- #
    # Evrende OLMAYAN bir kenarı dışlamak paydayı elle küçültme girişimidir.
    f10 = _run_fixture(_fixture_chain(),
                       exclusions=["mate:hayali_kenar:bahane"])
    assert f10["status"] == INVALID_COVERAGE, f10["status"]
    assert status_exit_code(f10["status"]) == 2
    assert UNKNOWN_EXCLUSION in _codes(f10)

    # ---- Kapalı sözlük ve isim alanı ------------------------------------- #
    verdicts = {GREEN, RED, CONFIRM, SCREEN_ONLY, LOCAL_POLICY, "SIM_ONLY",
                NO_COV, "INVALID_METRIC", INVALID_COVERAGE, "INVALID_SPARAM",
                "INCONCLUSIVE", STALE, "GEOMETRY_PRECHECK_PASS", "TBD",
                "API_YOK"}
    assert len(verdicts) == 15
    for report in (good, f1, f3, f5, f7, f8, f10):
        assert report["status"] in verdicts, report["status"]
        assert report["domain_state"] in DOMAIN_ORDER
        assert report["domain_state"] not in verdicts, (
            "domain durumu gate.verdict sözlüğüne SIZAMAZ")

    # ---- L3 devri: limitin sahibi bu kapı, ölçümün değil ----------------- #
    for row in good["l3_handoff"]:
        assert row["owner"] == "arayuz-eslesme-kapisi", row
        authority = row["limit"]["authority"]
        assert authority["owner_skill"] != "l3-bringup-karakterizasyon", (
            "SELF_AUTHORED_LIMIT: ölçümü alan limiti yazamaz")
        assert row["fixture_fields"], row
        # Fixture alan adları konnektor/SKILL.md:200-203'ten YENİDEN KULLANILIR.
        assert set(row["fixture_fields"]) <= {
            "actual_connector_mpn", "actual_fpc_thickness",
            "datum_fixture_used", "insert_remove_without_force",
            "actuator_full_lock_visible", "post_mate_clearance_margin"}, row
    assert "L3" not in good["assurance_loop"], (
        "PHYSICAL_MEASUREMENT yayımlamayan bir skill L3 listeleyemez")

    # ---- Alias tablosu yasaktır ------------------------------------------ #
    aliased = _fixture_chain()
    face_b = aliased["chains"][0]["nodes"][1]["faces"][0]
    face_b["pin_map"] = [dict(p) for p in face_b["pin_map"]]
    face_b["pin_map"][1]["net"] = "MIPI_D0_P"      # aynı iletken, ikinci ad
    alias = _run_fixture(aliased)
    assert alias["status"] == RED and NET_NAME_MISMATCH in _codes(alias)
    assert PIN_ORDER_MISMATCH not in _codes(alias), (
        "farklı ad kümesi bir permütasyon değildir")

    # ---- DECLARED_NONMATCHING: bakıldı, ve yanlış olduğu beyan edildi ---- #
    declared = _fixture_chain()
    target = declared["chains"][0]["nodes"][1]["faces"][0]
    target["declared_nonmatching"] = True
    target["declared_nonmatching_note"] = "gercek SoM pinlerine KARSILIK GELMEZ"
    dn = _run_fixture(declared)
    assert dn["status"] == RED, dn["status"]
    assert dn["domain_state"] == DECLARED_NONMATCHING, dn["domain_state"]

    # ---- Satıcı harfi hiçbir kararı beslemez ----------------------------- #
    letters = _fixture_chain()
    letters["chains"][0]["interconnects"][0]["medium"]["vendor_contact_letter"] \
        = "B"      # Molex'te "B" diye bir tip YOKTUR
    assert _run_fixture(letters)["status"] == GREEN, (
        "satıcı harfi verdict'i değiştirmemeli")

    # ---- İç kenar beyan edilmezse NO_COVERAGE ---------------------------- #
    unmapped = _fixture_chain(mapping=None)
    unmapped["chains"][0]["interconnects"][0]["medium"]["pin_to_pin"] = []
    um = _run_fixture(unmapped)
    assert MAPPING_UNDECLARED in _codes(um), um["reason_codes"]
    assert um["status"] != GREEN

    # ---- connector_spec: OPSİYONEL AMA SESSİZ DEĞİL ----------------------- #
    # Eksik kanıt CONFIRM'dür, PASS değildir (coverage-gate.md:21).
    # `required: false` kapıyı deponun kendi verisine kapatmamak içindir;
    # çapraz doğrulamayı GEÇMİŞ saymak için değil.
    no_spec = _run_fixture(_fixture_chain(), spec=None)
    assert no_spec["status"] == CONFIRM, no_spec["status"]
    assert CONNECTOR_SPEC_ABSENT in _codes(no_spec), no_spec["reason_codes"]
    assert any(c["rule"] == "connector_spec" for c in no_spec["caps"])
    assert no_spec["excluded_checks"], "kapsam dışı bırakma KAYDEDİLMEDİ"
    # ...ve verilmesi GREEN'i geri açar: cap koşulsuz değil, ölçüme bağlı.
    assert good["status"] == GREEN
    # Çelişen bir connector_spec ölçülmüş bir uyuşmazlıktır -> RED. Kapsam dışı
    # bırakılmış bir FAIL atlanmaz: ölçülmüş bir uyuşmazlık sonradan kapsam
    # dışına alınamaz.
    contradiction = _run_fixture(_fixture_chain(),
                                 spec=_fixture_connector_spec(pitch=0.4))
    assert contradiction["status"] == RED, contradiction["status"]
    assert SPEC_CONTRADICTS_CHAIN in _codes(contradiction)

    # ---- Kural başına payda: her matris satırı kendi kapsamını taşır ------ #
    rc = good["rule_coverage"]
    assert rc["R1"]["scanned"] > 0 and rc["R1"]["status"] == PASS, rc["R1"]
    assert rc["R12"]["scanned"] > 0 and rc["R12"]["status"] == PASS, rc["R12"]
    # Ölçülmemiş bir kural scanned=0 + NO_COVERAGE'dır; ona bağlı bir assertion
    # coverage kapısında durur ve "ihlal yok" diye okunamaz.
    for rule_id, block in rc.items():
        if block["scanned"] == 0:
            assert block["status"] == NO_COVERAGE, (rule_id, block)
        assert block["violations"] <= block["scanned"], (rule_id, block)
    f6_rc = _run_fixture(_fixture_chain(reverse_b=True))["rule_coverage"]
    assert f6_rc["R11"]["violations"] > 0 and f6_rc["R12"]["violations"] > 0
    # F5/F6'nın pozitif tarafı kural başına paydada da görünür.
    assert f6_rc["R3"]["status"] == PASS and f6_rc["R4"]["status"] == PASS

    # ---- Makbuz: doğrulayıcının reddettiği bir makbuz kapı açmaz ---------- #
    receipt = build_receipt(good, ["--self-test"], start_utc="2026-08-12T00:00:00Z",
                            end_utc="2026-08-12T00:00:01Z", exit_code=0)
    for field in ("schema_version", "gate_id", "verdict", "tool", "argv",
                  "started_utc", "finished_utc", "exit_code", "inputs",
                  "parsed_metrics", "metric_semantics", "coverage",
                  "retention"):
        assert field in receipt, f"makbuzda {field} yok"
    # parsed_metrics ile coverage.per_metric AYNI anahtar kümesi olmak zorunda.
    assert set(receipt["parsed_metrics"]) == set(receipt["coverage"]["per_metric"])
    # Her leaf bir TAMSAYI: `null` INVALID_METRIC'tir, `0` ise boş bir PASS
    # üretir. Ölçülmemiş metrik makbuza HİÇ girmez.
    for name, value in receipt["parsed_metrics"].items():
        assert isinstance(value, int) and not isinstance(value, bool), (name, value)
    no_coverage = {f["check"] for f in good["findings"]
                   if f["status"] == NO_COVERAGE}
    assert not (no_coverage & set(receipt["parsed_metrics"])), (
        "ölçülmemiş bir bulgu makbuza sızdı")
    assert receipt["blockers"] == [], "GREEN makbuzda blocker olamaz"
    assert all(isinstance(b, dict) for b in receipt["blockers"])
    red_receipt = build_receipt(f1, ["--self-test"], start_utc="2026-08-12T00:00:00Z",
                               end_utc="2026-08-12T00:00:01Z", exit_code=1)
    assert red_receipt["verdict"] == RED and red_receipt["blockers"]
    assert all(set(b) == {"code", "message"} for b in red_receipt["blockers"])

    diag("SELF-TEST PASS: payda interface_contract'tan türüyor ve zincir onu "
         "beyan edemiyor (F2), eksik halka adıyla RED veriyor (F1), pitch "
         "bozulurken R4 PASS kalıyor (F5), permütasyon PIN_ORDER_MISMATCH + "
         "POWER_ON_SIGNAL_PIN veriyor (F6), türetilmiş veri GREEN üretmiyor "
         "(F7), revizyonsuz provenans en fazla CONFIRM (F8), girdi değişimi "
         "STALE (F9), hayali dışlama INVALID_COVERAGE/exit 2 (F10), gerekçesiz "
         "dışlama nüfustan çıkarmıyor (F4), scanned=0 NO_COVERAGE (F3) ve "
         "BOZULMAMIŞ fixture GREEN veriyor")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
