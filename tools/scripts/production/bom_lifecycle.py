#!/usr/bin/env python3
"""BOM lifecycle / supply-risk gate: turns supply data into a
schema-conformant ``lifecycle_report.json`` and REFUSES to guess.

backend: cli  (network: NONE - see below)

WHY THIS TOOL DOES NOT TOUCH THE NETWORK
-------------------------------------------------------------------------
``skills/bom-lifecycle`` was carrying a dangling promise: its front matter
declared ``tools: [bom_lifecycle_koprusu]`` while no such tool existed in
this bundle. The archive module of that name was rejected during the port
as "network dependent, never run".

That rejection was measured and found WRONG, the same way
``mekanik_dxf_koprusu``'s was. Two reasons:

  1. The module's substance is OFFLINE - lifecycle gating, risk scoring,
     alternate acceptance. Exactly one function talked to a network, and it
     already fail-closed to ``TBD`` without a key.
  2. The skill's own contract says the tool must NOT make that call:
     ``lifecycle_report.schema.json`` defines
     ``provider.queried_from: main_agent | subagent | cache | unavailable``
     with the comment *"Network calls belong to the main agent. A sandboxed
     subagent silently fabricates."*

So the split is deliberate: the AGENT fetches (Nexar/Octopart, a
distributor API, a cached export) and hands the result in as
``--supply``; this tool validates, scores, gates and reports. Nothing here
invents an MPN, a price, a stock figure or a lifecycle state. A line with
no supply data is CONFIRM - never a silent "Active".

GATES (each traceable to the schema or MASTER_RULEBOOK Bölüm 0)
-------------------------------------------------------------------------
  * NRND / EOL / Obsolete       -> line RED. "BOM Kuralı (Kalıcı,
    İstisnasız)": no prototype exception. The rule exists because of real
    ESP32-C3FN4 (EOL) and MPU-6050 (Obsolete) selections.
  * lifecycle unknown / no data -> CONFIRM, and it still costs risk score
    (0.4). "Unknown" counted as zero is how an EOL part enters a design.
  * PCN: a set ``pdn_id_or_none``, or EOL with no ``last_time_buy`` /
    ``last_ship`` -> RED (schema $comment).
  * critical line with fewer than two sources -> violation.
  * alternate: ANY null in the 9-field ``provenance`` block caps it at
    CONFIRM; ``compatibility.land_pattern == "different"`` forces RED and
    sets ``opens_footprint_feedback`` - a footprint change is a layout
    feedback edge, not a BOM swap.
  * staleness: ``--stage quote`` treats data older than 7 days as stale,
    ``--stage review`` 30 days (the schema's own thresholds).

RISK SCORE - A RESOLVED CONFLICT, NOT A SILENT PICK
-------------------------------------------------------------------------
The archive scorer deliberately let the weights sum ABOVE 1.0 ("multiple
risk factors stack"). The schema constrains ``risk_score`` to
``maximum: 1``. Both cannot hold, so: the emitted number is CLAMPED to 1.0
(the contract is authoritative in this bundle) and the raw sum is preserved
verbatim in ``risk_reasons`` so the stacking is not lost.

Usage:
    bom_lifecycle.py <bom.json> [--supply supply.json] [--stage quote|review]
                     [--now ISO8601] [--json out.json]
    bom_lifecycle.py --self-test

Exit: 0 GREEN/CONFIRM/STALE, 1 RED/NO_COVERAGE, 2 crash (run_cli).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contract import ContractError, require_dict, require_list  # noqa: E402
from common.findings import coverage_status  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "lifecycle-report/v1"
TOOL_NAME = "bom_lifecycle"

# The schema's vocabulary, not the archive's ("TBD" there, "Unknown" here).
LIFECYCLE_VALUES = ("Active", "NRND", "EOL", "Obsolete", "Unknown")
BLOCKING_LIFECYCLES = ("NRND", "EOL", "Obsolete")

# Weights from the archive scorer, kept verbatim - they are not exact
# science, they encode MASTER_RULEBOOK's "keep >=2 sources on a critical
# part" rule. "Unknown" is 0.4 ON PURPOSE: an unverified part is a risk,
# and scoring it 0 is what lets an EOL part through unnoticed.
LIFECYCLE_WEIGHT = {
    "Active": 0.0, "NRND": 0.6, "EOL": 1.0, "Obsolete": 1.0, "Unknown": 0.4,
}
LOW_STOCK_THRESHOLD = 1000
LONG_LEAD_TIME_WEEKS = 26.0
ALTERNATE_SEARCH_THRESHOLD = 0.5

# The schema's own staleness comment: 7 days before a production quote,
# 30 days at design review.
STALENESS_DAYS = {"quote": 7, "review": 30}

PROVENANCE_FIELDS = (
    "source_url", "manufacturer_document_id", "datasheet_revision", "package_code",
    "package_document_revision", "land_pattern_source", "land_pattern_revision",
    "density_level_or_mfg_recommended", "zero_rotation_checked",
)
COMPATIBILITY_FIELDS = (
    "pin_function_and_order", "land_pattern", "abs_max_and_operating",
    "power_dissipation", "thermal_path",
)
PCN_FIELDS = ("pcn_policy_source", "pcn_id_or_none", "pdn_id_or_none",
              "last_time_buy", "last_ship")
# The report object and every block inside it is ``additionalProperties:
# false``. Supply data comes from a distributor API whose payload carries far
# more keys than this contract, so the pass-through blocks are FILTERED, not
# copied - an unfiltered copy makes the artifact schema-invalid, which was
# measured against the real schema rather than assumed.
ASSEMBLY_GATE_FIELDS = ("msl_level", "floor_life_hours", "packaging", "moq",
                        "true_cost_currency", "true_cost_moq_times_unit")


# --------------------------------------------------------------------------- #
# 1. Input reading - a malformed BOM is an error, never an empty PASS
# --------------------------------------------------------------------------- #
def read_bom(payload: object) -> List[dict]:
    """Normalises a BOM document into ``[{refdes: [...], mpn, ...}]``.

    Accepts either a bare list of lines or ``{"lines": [...]}``. A line's
    ``refdes`` may be a string or a list; both become a list because the
    schema requires ``minItems: 1``.

    A line with no MPN is NOT dropped: it becomes an excluded line with a
    reason. Dropping it would shrink ``eligible`` and quietly improve the
    coverage ratio - the "scanned=0 => never GREEN" principle applies to
    the denominator too.
    """
    if isinstance(payload, dict):
        lines = require_list(payload.get("lines"), "bom.lines", nonempty=False)
    else:
        lines = require_list(payload, "bom", nonempty=False)

    out: List[dict] = []
    for index, raw in enumerate(lines):
        line = require_dict(raw, f"bom.lines[{index}]")
        refdes = line.get("refdes")
        if isinstance(refdes, str):
            refdes = [refdes]
        if not isinstance(refdes, list) or not refdes:
            raise ContractError(f"bom.lines[{index}].refdes must be a non-empty string or list")
        mpn = line.get("mpn")
        if mpn is not None and not isinstance(mpn, str):
            raise ContractError(f"bom.lines[{index}].mpn must be a string or null")
        out.append({
            "refdes": [str(r) for r in refdes],
            "mpn": mpn,
            "manufacturer": line.get("manufacturer"),
            "critical": bool(line.get("critical", False)),
            "dnp": line.get("dnp"),
        })
    return out


def read_supply(payload: Optional[object]) -> Tuple[dict, dict, Optional[str]]:
    """``(provider, per-MPN entries, supply_checked_at)``.

    Missing file -> ``queried_from="unavailable"`` and no entries, which
    puts every line in CONFIRM. That is the honest state of a run with no
    supply data, and it is NOT the same as GREEN.
    """
    if payload is None:
        return ({"name": "none", "queried_from": "unavailable", "api_reachable": None}, {}, None)

    doc = require_dict(payload, "supply")
    provider = require_dict(doc.get("provider", {}), "supply.provider") if doc.get("provider") else {}
    name = provider.get("name") or "unknown"
    queried_from = provider.get("queried_from") or "cache"
    if queried_from not in ("main_agent", "subagent", "cache", "unavailable"):
        raise ContractError(
            f"supply.provider.queried_from={queried_from!r} is not one of "
            "main_agent/subagent/cache/unavailable")
    entries = require_dict(doc.get("parts", {}), "supply.parts") if doc.get("parts") else {}
    return ({"name": name, "queried_from": queried_from,
             "api_reachable": provider.get("api_reachable")},
            entries, doc.get("supply_checked_at"))


# --------------------------------------------------------------------------- #
# 2. Risk score
# --------------------------------------------------------------------------- #
def risk_score(line: dict, supply: dict) -> Tuple[Optional[float], List[str]]:
    """``(clamped score, reasons)``; ``(None, [...])`` when nothing is known.

    ``None`` is not zero. A line with no supply data at all has an UNKNOWN
    risk, and the schema allows null exactly so that "we did not measure
    this" cannot be written down as "we measured it and it was fine".
    """
    lifecycle = supply.get("lifecycle")
    if not supply:
        return None, ["tedarik verisi yok - CONFIRM"]
    if lifecycle not in LIFECYCLE_VALUES:
        lifecycle = "Unknown"

    reasons: List[str] = []
    score = LIFECYCLE_WEIGHT[lifecycle]
    if lifecycle != "Active":
        reasons.append(f"lifecycle={lifecycle}")

    stock = supply.get("stock_qty")
    if isinstance(stock, int) and not isinstance(stock, bool) and stock < LOW_STOCK_THRESHOLD:
        score += 0.3
        reasons.append(f"düşük stok ({stock} < {LOW_STOCK_THRESHOLD})")

    sources = supply.get("sources") or []
    if isinstance(sources, list):
        if len(sources) <= 1:
            score += 0.3
            reasons.append(f"single-source ({len(sources)} kaynak)")
        elif line.get("critical") and len(sources) < 2:
            score += 0.2
            reasons.append("kritik parça, <2 kaynak")

    lead = supply.get("lead_time_weeks")
    if isinstance(lead, (int, float)) and not isinstance(lead, bool) and lead > LONG_LEAD_TIME_WEEKS:
        score += 0.2
        reasons.append(f"uzun lead-time ({lead} hafta)")

    raw = round(score, 3)
    if raw > 1.0:
        # Kept, not discarded: the schema caps the field, the stacking is
        # real information and belongs in the record.
        reasons.append(f"ham skor {raw} (>1.0, şema gereği 1.0'a kırpıldı)")
    return min(raw, 1.0), reasons


# --------------------------------------------------------------------------- #
# 3. Alternate acceptance
# --------------------------------------------------------------------------- #
def judge_alternate(candidate: dict) -> dict:
    """A schema-shaped alternate with its verdict decided here, not copied
    from whatever the caller claimed.

    GREEN needs manufacturer-stated pin-to-pin compatibility AND an
    unchanged land pattern AND a fully-sourced provenance block. Marketing
    "pin-compatible" text is never evidence - and a different land pattern
    is a layout change, so it is RED with a footprint feedback edge rather
    than a BOM-level swap.
    """
    provenance = {field: candidate.get("provenance", {}).get(field)
                  for field in PROVENANCE_FIELDS}
    compatibility = {field: (candidate.get("compatibility", {}).get(field) or "unknown")
                     for field in COMPATIBILITY_FIELDS}
    for optional in ("dielectric", "v_rated_v", "case_size", "msl_changed", "packaging_changed"):
        if optional in candidate.get("compatibility", {}):
            compatibility[optional] = candidate["compatibility"][optional]

    missing_provenance = [f for f in PROVENANCE_FIELDS if provenance[f] is None]
    land_changed = compatibility["land_pattern"] == "different"

    if land_changed:
        verdict, reason = "RED", "land_pattern=different -> footprint/DFM geri besleme kenarı açılır"
    elif (compatibility["pin_function_and_order"] == "same"
          and compatibility["land_pattern"] == "same"
          and compatibility["abs_max_and_operating"] == "same_or_better"
          and compatibility["power_dissipation"] == "same_or_better"
          and compatibility["thermal_path"] == "same_or_better"
          and not missing_provenance):
        verdict, reason = "GREEN", "pin/land/abs-max/güç/termal aynı veya daha iyi, provenance tam"
    else:
        parts = []
        if missing_provenance:
            parts.append(f"provenance eksik: {missing_provenance}")
        unknowns = [f for f in COMPATIBILITY_FIELDS if compatibility[f] == "unknown"]
        if unknowns:
            parts.append(f"uyumluluk bilinmiyor: {unknowns}")
        worse = [f for f in COMPATIBILITY_FIELDS if compatibility[f] in ("different", "worse")]
        if worse:
            parts.append(f"daha kötü/farklı: {worse}")
        verdict, reason = "CONFIRM", "; ".join(parts) or "kanıt yetersiz"

    return {
        "mpn": candidate.get("mpn") or "",
        "manufacturer": candidate.get("manufacturer"),
        "provenance": provenance,
        "compatibility": compatibility,
        "verdict": verdict,
        "verdict_reason": reason,
        "opens_footprint_feedback": land_changed,
    }


# --------------------------------------------------------------------------- #
# 4. PCN gate
# --------------------------------------------------------------------------- #
def pcn_block(supply: dict) -> dict:
    raw = supply.get("pcn") or {}
    return {field: raw.get(field) for field in PCN_FIELDS} | {
        "affects_fit_form_function": raw.get("affects_fit_form_function")}


def pcn_violations(pcn: dict, lifecycle: Optional[str]) -> List[str]:
    """Schema $comment: a set PDN, or EOL with no last-time-buy / last-ship,
    is RED. An EOL part whose end dates are unknown cannot be planned around
    at all, which is worse than an EOL part with a known buy window."""
    problems: List[str] = []
    if pcn.get("pdn_id_or_none"):
        problems.append(f"PDN yayınlanmış ({pcn['pdn_id_or_none']})")
    if lifecycle == "EOL" and not (pcn.get("last_time_buy") or pcn.get("last_ship")):
        problems.append("EOL ama last_time_buy/last_ship bilinmiyor")
    return problems


# --------------------------------------------------------------------------- #
# 5. One BOM line -> one schema line
# --------------------------------------------------------------------------- #
def build_line(line: dict, supply: dict) -> dict:
    violations: List[str] = []
    lifecycle = supply.get("lifecycle") if supply else None
    if lifecycle is not None and lifecycle not in LIFECYCLE_VALUES:
        lifecycle = "Unknown"

    score, reasons = risk_score(line, supply)
    alternates = [judge_alternate(a) for a in (supply.get("alternates") or [])]
    pcn = pcn_block(supply)
    pcn_problems = pcn_violations(pcn, lifecycle)
    violations.extend(pcn_problems)

    sources = supply.get("sources") or []
    if line.get("critical") and len(sources) < 2:
        violations.append(f"kritik parça {len(sources)} kaynakla (>=2 gerekli)")

    if line["mpn"] is None:
        status, uncovered = "NO_COVERAGE", "satırda MPN yok - taranamaz"
    elif not supply:
        status, uncovered = "CONFIRM", "tedarik verisi yok - ana ajan sorgulamalı"
    elif lifecycle in BLOCKING_LIFECYCLES:
        status = "RED"
        uncovered = None
        violations.append(
            f"lifecycle={lifecycle} - MASTER_RULEBOOK Bölüm 0 'BOM Kuralı "
            "(Kalıcı, İstisnasız)': prototip istisnası YOK, Active alternatife geçilmeli")
    elif pcn_problems:
        status, uncovered = "RED", None
    elif lifecycle == "Unknown":
        status, uncovered = "CONFIRM", "lifecycle doğrulanmadı"
    elif violations:
        status, uncovered = "RED", None
    else:
        status, uncovered = "GREEN", None

    return {
        "refdes": line["refdes"],
        "mpn": line["mpn"],
        "manufacturer": line.get("manufacturer") or supply.get("manufacturer"),
        "lifecycle": lifecycle,
        "risk_score": score,
        "stock_qty": supply.get("stock_qty"),
        "lead_time_weeks": supply.get("lead_time_weeks"),
        "sources": [str(s) for s in sources] if isinstance(sources, list) else [],
        "assembly_gates": {field: value for field, value
                           in (supply.get("assembly_gates") or {}).items()
                           if field in ASSEMBLY_GATE_FIELDS},
        "pcn": pcn,
        "dnp": line.get("dnp"),
        "alternates": alternates,
        "status": status,
        "uncovered_reason": uncovered,
        # Not part of the schema's line object - carried alongside for the
        # human-facing summary and stripped before the report is emitted.
        "_risk_reasons": reasons,
        "_violations": violations,
    }


def alternate_advice(line: dict) -> str:
    """The archive's ``alternatif_karari_ozetle`` in schema terms: what a
    reader should DO with this line. An empty accepted-candidate list under
    a high risk score is NEEDS_HUMAN - never an automatic "closest match"."""
    score = line["risk_score"]
    if score is None:
        return f"{line['mpn']}: risk ölçülemedi (tedarik verisi yok) -> CONFIRM."
    if score <= ALTERNATE_SEARCH_THRESHOLD and line["status"] != "RED":
        return f"{line['mpn']}: risk düşük ({score}), alternatif aranmadı."
    accepted = [a["mpn"] for a in line["alternates"] if a["verdict"] == "GREEN"]
    if not accepted:
        return (f"{line['mpn']}: risk yüksek ({score}, {line['_risk_reasons']}) "
                "AMA kabul edilebilir alternatif yok -> NEEDS_HUMAN.")
    feedback = [a["mpn"] for a in line["alternates"] if a["opens_footprint_feedback"]]
    extra = (f" UYARI: {feedback} footprint değiştiriyor -> stackup/routing geri beslemesi aç."
             if feedback else "")
    return f"{line['mpn']}: risk yüksek ({score}), kabul edilen alternatif(ler): {accepted}.{extra}"


# --------------------------------------------------------------------------- #
# 6. Staleness
# --------------------------------------------------------------------------- #
def staleness(supply_checked_at: Optional[str], stage: str,
              now: Optional[datetime] = None) -> Optional[str]:
    """``None`` when fresh, else the reason it is stale.

    An unparsable or missing timestamp is treated as STALE, not fresh: the
    whole point of the field is to stop a six-month-old stock figure being
    read as today's.
    """
    limit = STALENESS_DAYS[stage]
    if not supply_checked_at:
        return "supply_checked_at yok - tazelik doğrulanamıyor"
    try:
        checked = datetime.fromisoformat(str(supply_checked_at).replace("Z", "+00:00"))
    except ValueError:
        return f"supply_checked_at okunamadı ({supply_checked_at!r})"
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    age = reference - checked
    if age > timedelta(days=limit):
        return f"tedarik verisi {age.days} günlük (>{limit}, aşama={stage})"
    return None


# --------------------------------------------------------------------------- #
# 7. The report
# --------------------------------------------------------------------------- #
def build_report(bom: List[dict], supply_doc: Optional[object] = None,
                 stage: str = "review", now: Optional[datetime] = None) -> dict:
    provider, entries, checked_at = read_supply(supply_doc)
    lines = [build_line(line, require_dict(entries.get(line["mpn"]) or {}, "supply.parts[*]"))
             for line in bom]

    eligible = len(lines)
    scanned = sum(1 for line in lines if line["status"] != "NO_COVERAGE")
    violations = sum(1 for line in lines if line["status"] == "RED")
    coverage = {"eligible": eligible, "scanned": scanned,
                "excluded": eligible - scanned, "violations": violations}

    stale_reason = staleness(checked_at, stage, now)
    base = coverage_status(coverage)  # INVALID_COVERAGE / NO_COVERAGE / RED / GREEN
    if base in ("NO_COVERAGE", "RED"):
        status = base
    elif base == "INVALID_COVERAGE":  # pragma: no cover - arithmetic is built here
        raise ContractError(f"coverage bloğu tutarsız: {coverage}")
    elif any(line["status"] == "CONFIRM" for line in lines):
        status = "CONFIRM"
    elif stale_reason:
        status = "STALE"
    else:
        status = "GREEN"

    advice = [alternate_advice(line) for line in lines if line["mpn"]]
    for line in lines:
        line.pop("_risk_reasons", None)
        line.pop("_violations", None)

    return {
        "schema_version": SCHEMA_VERSION,
        "supply_checked_at": checked_at,
        "provider": provider,
        "lines": lines,
        "coverage": coverage,
        "status": status,
        # Outside the report contract (``additionalProperties: false`` covers
        # the report object, so these travel in the CLI envelope, not in the
        # artifact written by --json).
    }, {"stale_reason": stale_reason, "advice": advice}


# --------------------------------------------------------------------------- #
# 8. CLI
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    bom = read_bom([
        {"refdes": "U1", "mpn": "ESP32-C3FN4", "critical": True},
        {"refdes": ["C1", "C2"], "mpn": "GRM155R60J105KE19D"},
        {"refdes": "R1", "mpn": None},
    ])
    assert len(bom) == 3 and bom[1]["refdes"] == ["C1", "C2"]

    report, extra = build_report(bom, {
        "supply_checked_at": "2026-08-07T00:00:00Z",
        "provider": {"name": "nexar", "queried_from": "main_agent", "api_reachable": True},
        "parts": {
            "ESP32-C3FN4": {"lifecycle": "EOL", "stock_qty": 12, "sources": ["mouser"],
                            "pcn": {"pdn_id_or_none": None, "last_time_buy": None,
                                    "last_ship": None}},
            "GRM155R60J105KE19D": {"lifecycle": "Active", "stock_qty": 500000,
                                   "sources": ["digikey", "mouser", "lcsc"]},
        },
    }, stage="review", now=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc))

    eol = report["lines"][0]
    assert eol["status"] == "RED", eol
    assert eol["risk_score"] == 1.0, eol["risk_score"]  # clamped from 1.6
    assert report["lines"][1]["status"] == "GREEN", report["lines"][1]
    assert report["lines"][2]["status"] == "NO_COVERAGE"
    assert report["coverage"] == {"eligible": 3, "scanned": 2, "excluded": 1, "violations": 1}
    assert report["status"] == "RED"

    # No supply data must never read as GREEN.
    empty, _ = build_report(bom[:2], None, stage="review")
    assert empty["status"] == "CONFIRM", empty["status"]
    assert empty["provider"]["queried_from"] == "unavailable"

    # A land-pattern change is a layout feedback edge, not a swap.
    alt = judge_alternate({"mpn": "X", "compatibility": {"land_pattern": "different",
                                                         "pin_function_and_order": "same"}})
    assert alt["verdict"] == "RED" and alt["opens_footprint_feedback"]

    assert staleness("2026-01-01T00:00:00Z", "quote",
                     datetime(2026, 8, 7, tzinfo=timezone.utc))
    assert staleness(None, "review") is not None, "eksik zaman damgası taze sayılamaz"

    diag("SELF-TEST PASS: lifecycle gate, risk clamp, coverage arithmetic, "
        "alternate verdicts and staleness all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bom", nargs="?", help="bom.json (refdes/mpn lines)")
    ap.add_argument("--supply", help="supply data FETCHED BY THE AGENT (this tool never queries)")
    ap.add_argument("--stage", default="review", choices=sorted(STALENESS_DAYS))
    ap.add_argument("--now", help="ISO-8601 reference time for staleness (testing/reproducibility)")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.bom:
        ap.error("bom.json is required unless --self-test is given")

    bom = read_bom(json.loads(Path(a.bom).read_text(encoding="utf-8")))
    supply = (json.loads(Path(a.supply).read_text(encoding="utf-8")) if a.supply else None)
    now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else None

    report, extra = build_report(bom, supply, stage=a.stage, now=now)
    write_report(report, a.json)
    emit({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME, "status": report["status"],
          "coverage": report["coverage"], "stale_reason": extra["stale_reason"],
          "advice": extra["advice"]})
    return 1 if report["status"] in ("RED", "NO_COVERAGE") else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
