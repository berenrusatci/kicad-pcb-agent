#!/usr/bin/env python3
"""ECAD asset qualification: the symbol+footprint pair is not released for
use until every pin, pad and package attribute has been bound - BY NAME,
NUMBER AND FUNCTION - to an authoritative pin/package record.

backend: cli  (network: NONE)

WHY THIS TOOL EXISTS - THE COUNT GATE IS NOT A SEMANTIC GATE
-------------------------------------------------------------------------
``cad_assets.py`` says it in its own header: a symbol from one package
variant paired with a footprint from another "passes ERC and DRC and is
only discovered when the first board is soldered". That is the failure the
bundle already named out loud.

Then it was measured, and the gate that was supposed to catch it does this
and only this (``cad_assets.py:177-190``, ``check_pin_count``):

    1. len(symbol pin numbers) == datasheet_pin_count
    2. len(footprint pad numbers) == len(symbol pin numbers)

Two COUNT comparisons. Everything below passes it cleanly:

  * a 6-pin symbol whose pin 1 is VIN and pin 3 is GND, against a datasheet
    where pin 1 is GND and pin 3 is VIN - same six numbers, swapped
    functions, and the first power-up is a short across the regulator;
  * a symbol whose pin 4 is named ``EN`` and typed ``input`` against a part
    whose pin 4 is ``NC`` - the count is identical and the net goes to a
    pin that is internally unconnected on this variant;
  * a QFN-40 footprint substituted for the QFN-40 of a DIFFERENT package
    family with the same pad count and a 0.5 mm pitch instead of 0.4 mm;
  * an ``EP`` pad present in the footprint under a number the datasheet
    never assigns, which balances the counts while the real thermal pad is
    missing.

``check_pin_count`` is not wrong; it is a cardinality check, and equal
cardinality is not equal identity. This tool is the SEMANTIC layer ON TOP
of it - it runs ``check_pin_count`` underneath (imported, not
reimplemented) and then asks the question the counts cannot answer: is
each individual pin, pad and package attribute the one the manufacturer
document actually specifies?

WHAT THIS TOOL DOES NOT DO
-------------------------------------------------------------------------
  * IT DOES NOT SELECT A PART. ``bom_lifecycle`` owns MPN, lifecycle and
    alternates. The MPN arrives as an input. The only lifecycle judgement
    made here is a REFUSAL: ``cad_assets.lifecycle_gate`` is called, and a
    blocking state means the asset is not released - the state itself is
    read from ``lifecycle_report.json``, never decided here.
  * IT DOES NOT CONNECT NETS. ``sematik-erc`` owns wire/net topology. This
    tool never opens a schematic and never asserts what a pin is wired to.
  * IT DOES NOT JUDGE MANUFACTURABILITY. NSMD vs SMD, EPAD paste coverage,
    mask dam and every fab geometry rule belong to ``dfm``
    (``dfm/SKILL.md`` QFN/EPAD and NSMD sections). A pad here is compared
    to the authoritative land pattern for IDENTITY, not for whether it can
    be soldered. A footprint can be fully qualified by this tool and still
    be rejected by DFM, and that is the correct division.
  * IT DOES NOT EXTRACT A DATASHEET. There is no PDF parsing, no OCR and
    no heuristic table scraping. The authoritative pin/package record is a
    structured input that a human or a vendor authored and SIGNED with
    provenance. See the open question below.
  * IT DOES NOT INVENT A NUMBER. A dimension the source does not state is
    an EXCLUDED attribute with a reason, never a default. No fallback
    tolerance, no assumed pitch, no "typical" span.
  * IT MAKES NO NETWORK CALL. ``jlc_library`` / ``cad_assets`` bring the
    candidate files in; this tool only reads what is already on disk.

WHAT IT CANNOT PROVE (these are permanent, not TODOs)
-------------------------------------------------------------------------
  * If the manufacturer document does not state a pin's function or the
    package table unambiguously, software cannot upgrade that to GREEN.
    The verdict is ``CONFIRM`` and a human reads the document.
  * Solderability, wetting and assembly yield are not file properties.
    Comparing two files proves nothing about a joint; that is first-article
    (L3) evidence and it is not produced here.
  * When a manufacturer application note contradicts a generic footprint
    rule, the manufacturer wins and this tool records the conflict. It does
    not synthesise a compromise geometry.

OPEN QUESTION, DELIBERATELY LEFT OPEN
-------------------------------------------------------------------------
Which artifact is the canonical repo-wide authoritative pin/package source
for ECAD qualification is NOT decided. Inventing a datasheet-extraction
format here and then treating its output as ground truth would be the
fabricated-source failure this whole bundle is built against. So the input
contract (``skills/ecad-varlik-kalifikasyonu/references/
pin_package_source.schema.json``) is an EXPLICITLY DECLARED, human- or
vendor-authored record that must carry
``document_id`` + ``revision`` + ``locator`` + ``accessed`` +
``snapshot_sha256`` (``skills/_shared/coverage-gate.md``,
``evidence_origin`` table, ``AUTHORITATIVE_SOURCE`` row). Until the
canonical artifact is chosen, a run whose source is not provenance-complete
reaches ``CONFIRM`` at best and can never reach ``GREEN``.

Usage:
    cad_asset_qualification.py --source pin_package_source.json
                              --symbol part.kicad_sym
                              --footprint part.kicad_mod
                              [--lifecycle lifecycle_report.json]
                              [--source-snapshot datasheet.pdf]
                              [--prior previous_qualification.json]
                              [--json cad_asset_qualification.json]
                              [--receipt cad_asset_gate_receipt.json]
                              [--register --project DIR --lib-name NICK]
    cad_asset_qualification.py --self-test

Exit: 0 GREEN, 1 RED/CONFIRM/STALE/LOCAL_POLICY/SCREEN_ONLY/NO_COVERAGE,
      2 INVALID_COVERAGE or crash (run_cli).

The exit contract DELIBERATELY differs from ``bom_lifecycle``, which exits
0 on CONFIRM/STALE. This is a release gate: anything that is not GREEN must
keep the door shut, and an exit code a caller reads as success would open
it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.contract import ContractError, require_dict  # noqa: E402
from common.findings import (  # noqa: E402
    FAIL, NO_COVERAGE, by_check_map, coverage_rule_violations, result,
    rollup_status, summarise,
)
from common.hashing import sha256_file, sha256_text  # noqa: E402
from common.report_io import (  # noqa: E402
    diag, emit, run_cli, status_exit_code, write_report,
)
from common.sexpr import find, parse_sexpr, unquote  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cad_assets import (  # noqa: E402
    check_pin_count, footprint_pad_numbers, lifecycle_gate, register_assets,
    symbol_pin_numbers,
)

SCHEMA_VERSION = "cad-asset-qualification-report/v1"
RECEIPT_SCHEMA_VERSION = "gate-receipt/v1"
SOURCE_SCHEMA_VERSION = "pin-package-source/v1"
TOOL_NAME = "cad_asset_qualification"

# skills/_shared/coverage-gate.md, evidence_origin table, AUTHORITATIVE_SOURCE
# row. All five, or the record is not an authoritative source - it is a note.
PROVENANCE_FIELDS: Tuple[str, ...] = (
    "document_id", "revision", "locator", "accessed", "snapshot_sha256",
)

# The verdict vocabulary is CLOSED (skills/_shared/coverage-gate.md). This
# tool adds nothing to it; every name below already exists there and in
# gate_receipt.schema.json's verdict enum.
GREEN = "GREEN"
RED = "RED"
CONFIRM = "CONFIRM"
SCREEN_ONLY = "SCREEN_ONLY"
LOCAL_POLICY = "LOCAL_POLICY"
STALE = "STALE"
INVALID_COVERAGE = "INVALID_COVERAGE"

# Cap precedence. A cap never opens a gate; it says HOW FAR from GREEN the
# run is and why. CONFIRM outranks the others because it is the one that
# says "a human must read a document", which is strictly more work than
# "this fallback is not a sign-off".
CAP_ORDER: Tuple[str, ...] = (CONFIRM, LOCAL_POLICY, SCREEN_ONLY)

# KiCad's electrical pin types, verbatim. The authoritative record must use
# these tokens because that is what the symbol file contains; translating
# datasheet prose into one of them is the SOURCE AUTHOR's declaration, not
# something this tool guesses.
ELECTRICAL_ROLES: Tuple[str, ...] = (
    "input", "output", "bidirectional", "tri_state", "passive", "free",
    "unspecified", "power_in", "power_out", "open_collector",
    "open_emitter", "no_connect",
)

# A land pattern that came from the part manufacturer is the only one that
# can leave the verdict at GREEN. Everything else is recorded WITH its
# derivation and stays explicitly not-a-signoff.
LAND_PATTERN_AUTHORITATIVE = ("manufacturer",)
LAND_PATTERN_DERIVED = ("ipc_derived", "local_library", "vendor_ecad")

CHECK_PIN = "pin_kimlik_eslemesi"
CHECK_PAD = "pad_kimlik_eslemesi"
CHECK_PADMAP = "sembol_footprint_padmap"
CHECK_PACKAGE = "paket_kimligi"
CHECK_BASE = "taban_pin_sayisi"
CHECK_LIFECYCLE = "lifecycle_serbest_birakma"


# --------------------------------------------------------------------------- #
# 0. Soft readers - a missing input is an ANSWER (NO_COVERAGE), not a crash
# --------------------------------------------------------------------------- #
def read_json_soft(path: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """``(value, error)``. A missing or malformed file returns the reason.

    Deliberately soft. The single most likely real-world state of this gate
    is "the authoritative record does not exist yet", and that state must
    produce a receipt saying NO_COVERAGE rather than a stack trace - a
    crash leaves no artifact, and a gate with no artifact is a gate nobody
    can prove ran.
    """
    if path is None:
        return None, "yol verilmedi"
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{path}: dosya yok"
    except IsADirectoryError:
        return None, f"{path}: dizin, dosya değil"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, f"{path}: JSON okunamadı ({exc})"


def read_text_soft(path: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(text, error)`` for a candidate ``.kicad_sym`` / ``.kicad_mod``."""
    if path is None:
        return None, "yol verilmedi"
    try:
        return Path(path).read_text(encoding="utf-8"), None
    except FileNotFoundError:
        return None, f"{path}: dosya yok"
    except IsADirectoryError:
        return None, f"{path}: dizin, dosya değil"
    except UnicodeDecodeError as exc:
        return None, f"{path}: UTF-8 olarak okunamadı ({exc})"


def hashed(path: Optional[str]) -> Dict[str, Optional[str]]:
    """``{path, sha256}``; sha256 is None when the file cannot be hashed.

    Loud rather than absent, for the reason ``findings.board_report``
    states: a reader must SEE that the binding is missing instead of
    inferring it from a field that is not there.
    """
    if path is None:
        return {"path": None, "sha256": None}
    try:
        return {"path": str(path), "sha256": sha256_file(path)}
    except OSError:
        return {"path": str(path), "sha256": None}


# --------------------------------------------------------------------------- #
# 1. The authoritative record
# --------------------------------------------------------------------------- #
def provenance_missing(source: dict) -> List[str]:
    """Which AUTHORITATIVE_SOURCE fields are absent or empty."""
    prov = source.get("provenance")
    if not isinstance(prov, dict):
        return list(PROVENANCE_FIELDS)
    return [f for f in PROVENANCE_FIELDS
            if not str(prov.get(f) or "").strip()]


def source_pin_table(source: dict) -> List[dict]:
    entries = source.get("pins")
    if not isinstance(entries, list):
        return []
    return [p for p in entries if isinstance(p, dict) and str(p.get("number") or "").strip()]


def source_pad_table(source: dict) -> List[dict]:
    entries = source.get("pads")
    if not isinstance(entries, list):
        return []
    return [p for p in entries if isinstance(p, dict) and str(p.get("number") or "").strip()]


def accepted_exceptions(source: dict) -> Tuple[List[dict], List[dict]]:
    """Split ``exceptions`` into accepted and refused.

    AN EXCEPTION WITHOUT EVIDENCE IS NOT AN EXCEPTION. An accepted entry
    removes an item from the scanned population, which is exactly the move
    that turns a gate into a formality, so acceptance requires a non-empty
    reason AND a complete five-field provenance block of its own. A refused
    exception is not dropped: it is reported, and its item stays scanned -
    so writing an unevidenced exception makes the report LOUDER, never
    quieter.
    """
    accepted: List[dict] = []
    refused: List[dict] = []
    for raw in (source.get("exceptions") or []):
        if not isinstance(raw, dict):
            refused.append({"exception": raw, "refusal_reason":
                            "istisna kaydı bir nesne değil"})
            continue
        entry = {
            "scope": raw.get("scope"),
            "item": str(raw.get("item") or ""),
            "reason": str(raw.get("reason") or "").strip(),
            "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
        }
        gaps = [f for f in PROVENANCE_FIELDS
                if not str(entry["evidence"].get(f) or "").strip()]
        if entry["scope"] not in ("pin", "pad", "package_attribute"):
            entry["refusal_reason"] = f"scope tanımsız: {entry['scope']!r}"
            refused.append(entry)
        elif not entry["item"]:
            entry["refusal_reason"] = "item boş"
            refused.append(entry)
        elif not entry["reason"]:
            entry["refusal_reason"] = "reason boş - gerekçesiz istisna kabul edilmez"
            refused.append(entry)
        elif gaps:
            entry["refusal_reason"] = ("evidence eksik: " + ", ".join(gaps)
                                       + " - kanıtsız istisna kabul edilmez")
            refused.append(entry)
        else:
            entry["accepted"] = True
            accepted.append(entry)
    return accepted, refused


def exception_index(accepted: Sequence[dict], scope: str) -> Dict[str, str]:
    return {e["item"]: e["reason"] for e in accepted if e["scope"] == scope}


# --------------------------------------------------------------------------- #
# 2. Structural readers - the NESTING is the claim, never a regex
# --------------------------------------------------------------------------- #
def _walk(node, visit) -> None:
    if not isinstance(node, list):
        return
    visit(node)
    for item in node:
        _walk(item, visit)


def symbol_pins(symbol_text: str) -> Tuple[Dict[str, dict], List[str]]:
    """``({number: {name, electrical_role}}, inconsistencies)``.

    Read through ``common.sexpr`` for the reason ``cad_assets._numbers_under``
    gives: a regex cannot tell a pin's number from the same token appearing
    elsewhere, and the nesting is the thing being asserted.

    A pin form is ``(pin <electrical_type> <graphic_style> ...)``, so both
    tokens after ``pin`` are bare atoms. That shape also excludes the
    ``(pin "1" (uuid ...))`` instance forms in a ``.kicad_sch``, where the
    second element is a list.

    Multi-unit and De Morgan bodies repeat a pin; the repeats are collapsed
    the way ``cad_assets.symbol_pin_numbers`` collapses them. But a repeat
    that DISAGREES about the pin's name or electrical type is not a
    duplicate, it is a broken symbol, and it is returned as an
    inconsistency instead of being silently resolved to whichever body was
    parsed first.
    """
    tree = parse_sexpr(symbol_text)
    pins: Dict[str, dict] = {}
    inconsistent: List[str] = []
    if tree is None:
        return pins, inconsistent

    def visit(node: list) -> None:
        if not (len(node) >= 3 and node[0] == "pin"
                and isinstance(node[1], str) and isinstance(node[2], str)):
            return
        name: Optional[str] = None
        number: Optional[str] = None
        for item in node:
            if isinstance(item, list) and len(item) > 1 and isinstance(item[1], str):
                if item[0] == "name":
                    name = unquote(item[1])
                elif item[0] == "number":
                    number = unquote(item[1])
        if number is None:
            return
        entry = {"number": number, "name": name, "electrical_role": node[1]}
        prior = pins.get(number)
        if prior is None:
            pins[number] = entry
        elif (prior["name"], prior["electrical_role"]) != (name, entry["electrical_role"]):
            inconsistent.append(
                f"pin {number}: sembol içinde çelişkili tanım "
                f"({prior['name']}/{prior['electrical_role']} vs "
                f"{name}/{entry['electrical_role']})")

    _walk(tree, visit)
    return pins, inconsistent


def _pad_xy(node: list) -> Optional[Tuple[float, float]]:
    at = find(node, "at")
    if not at or len(at) < 3:
        return None
    try:
        return float(at[1]), float(at[2])
    except (TypeError, ValueError):
        return None


def footprint_pads(footprint_text: str) -> Tuple[Dict[str, dict], List[dict]]:
    """``({number: pad}, mechanical)`` from a ``.kicad_mod``.

    A pad form is ``(pad "N" <type> <shape> ...)``. Pads whose number is the
    empty string are mounting/mechanical pads: KiCad writes them exactly
    that way, they have no electrical identity, and comparing them against a
    pin table would manufacture violations. They are returned separately and
    counted as excluded, WITH the reason, rather than dropped.

    The exposed/thermal pad is NOT filtered - ``cad_assets`` explains why,
    and it is the single pad this gate most needs to see.
    """
    tree = parse_sexpr(footprint_text)
    pads: Dict[str, dict] = {}
    mechanical: List[dict] = []
    if tree is None:
        return pads, mechanical

    def visit(node: list) -> None:
        if not (len(node) >= 3 and node[0] == "pad"
                and isinstance(node[1], str) and isinstance(node[2], str)):
            return
        number = unquote(node[1])
        shape = node[3] if len(node) > 3 and isinstance(node[3], str) else None
        point = _pad_xy(node)
        if not number:
            mechanical.append({"type": node[2], "shape": shape, "at": point})
            return
        entry = pads.setdefault(number, {
            "number": number, "type": node[2], "shape": shape,
            "points": [], "occurrences": 0,
        })
        entry["occurrences"] += 1
        if point is not None:
            entry["points"].append(point)

    _walk(tree, visit)
    return pads, mechanical


def footprint_name(footprint_text: str) -> Optional[str]:
    """The ``(footprint "NAME" ...)`` token, or None."""
    tree = parse_sexpr(footprint_text)
    if isinstance(tree, list) and len(tree) > 1 and tree[0] == "footprint" \
            and isinstance(tree[1], str):
        return unquote(tree[1])
    return None


def pad_centre_span(pads: Dict[str, dict], epad_number: Optional[str]
                    ) -> Tuple[Optional[float], Optional[float], int]:
    """``(span_x, span_y, n)`` over pad CENTRES, excluding the exposed pad.

    CENTRELINE, not edge-gap, and the receipt says so in
    ``metric_semantics.distance_semantics``. Presenting one as the other is
    INVALID_METRIC under ``gate_receipt.schema.json``, and a land pattern
    table that quotes lead-tip extents would not compare to this number.

    The exposed pad is excluded because its centre sits inside the lead
    span; including it cannot widen the span but WOULD produce a bogus
    0 x 0 span for a package whose only pad is the EP.
    """
    points: List[Tuple[float, float]] = []
    for number, pad in pads.items():
        if epad_number is not None and number == epad_number:
            continue
        points.extend(pad["points"])
    if len(points) < 2:
        return None, None, len(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (round(max(xs) - min(xs), 4), round(max(ys) - min(ys), 4), len(points))


# --------------------------------------------------------------------------- #
# 3. Coverage rows
# --------------------------------------------------------------------------- #
def coverage_row(eligible: int, excluded: int, violations: int) -> Dict[str, int]:
    """``scanned`` is DERIVED, never passed in.

    ``scanned + excluded == eligible`` is then true by construction rather
    than by a caller remembering it, which is how the arithmetic breaks in
    practice. ``violations`` is clamped to ``scanned`` at the point where a
    clamp would hide something - it cannot, because every violation counter
    in this module counts ITEMS, one per scanned item, never messages.
    """
    scanned = eligible - excluded
    return {"eligible": eligible, "scanned": scanned, "excluded": excluded,
            "violations": violations}


def sum_coverage(rows: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    total = {"eligible": 0, "scanned": 0, "excluded": 0, "violations": 0}
    for row in rows.values():
        for key in total:
            total[key] += row[key]
    return total


def _sort_key(number: str):
    return (0, int(number), "") if number.isdigit() else (1, 0, number)


# --------------------------------------------------------------------------- #
# 4. The semantic checks
# --------------------------------------------------------------------------- #
def check_pin_identity(src_pins: List[dict], sym_pins: Dict[str, dict],
                       *, symbol_error: Optional[str],
                       exceptions: Dict[str, str],
                       inconsistencies: List[str]):
    """Per-pin number + name(function) + electrical role, both directions.

    THE UNIVERSE IS THE UNION of the authoritative numbers and the symbol's
    numbers. Using only the authoritative side would let a symbol carry
    extra pins the datasheet never defines and still report full coverage;
    using only the symbol side would miss a pin the part has and the symbol
    forgot. Both directions are the same failure wearing different clothes.

    NO NAME NORMALISATION IS APPLIED beyond stripping surrounding
    whitespace. No case folding, no ``~{X}`` overbar rewriting, no alias
    table. A normaliser is precisely where a wrong pin quietly becomes a
    right one, and the source author is the one who must declare the exact
    name the symbol is expected to carry.
    """
    numbers = sorted({str(p["number"]).strip() for p in src_pins}
                     | set(sym_pins), key=_sort_key)
    by_number = {str(p["number"]).strip(): p for p in src_pins}

    universe: List[dict] = []
    violations: List[str] = []
    bad = 0
    excluded = 0

    source_empty = not src_pins
    for number in numbers:
        declared = by_number.get(number)
        actual = sym_pins.get(number)
        entry = {
            "number": number,
            "name": (declared or {}).get("name"),
            "function": (declared or {}).get("function"),
            "electrical_role": (declared or {}).get("electrical_role"),
            "symbol_name": (actual or {}).get("name"),
            "symbol_electrical_role": (actual or {}).get("electrical_role"),
        }
        if source_empty:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = (
                "otoriter kaynak pin tablosu boş - karşılaştırılacak kayıt yok")
            excluded += 1
        elif symbol_error or not sym_pins:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = (
                symbol_error or "aday sembol okunamadı / pin içermiyor")
            excluded += 1
        elif number in exceptions:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = exceptions[number]
            excluded += 1
        elif declared is None:
            entry["status"] = "missing_in_source"
            violations.append(
                f"pin {number}: sembolde var, otoriter pin tablosunda YOK "
                f"(sembol adı={actual.get('name')!r})")
            bad += 1
        elif actual is None:
            entry["status"] = "missing_in_symbol"
            violations.append(
                f"pin {number}: otoriter tabloda var "
                f"(ad={declared.get('name')!r}, işlev={declared.get('function')!r}), "
                f"sembolde YOK")
            bad += 1
        else:
            problems: List[str] = []
            want_name = declared.get("name")
            if want_name is not None and str(want_name).strip() != str(actual.get("name") or "").strip():
                problems.append(
                    f"ad/işlev: beklenen {want_name!r}, sembolde {actual.get('name')!r}")
            want_role = declared.get("electrical_role")
            if want_role is not None and str(want_role).strip() != str(actual.get("electrical_role") or "").strip():
                problems.append(
                    f"elektriksel rol: beklenen {want_role!r}, "
                    f"sembolde {actual.get('electrical_role')!r}")
            if problems:
                entry["status"] = "mismatch"
                entry["mismatch"] = problems
                violations.append(f"pin {number}: " + "; ".join(problems))
                bad += 1
            else:
                entry["status"] = "matched"
        universe.append(entry)

    # A symbol that contradicts itself is counted against the pins that
    # carry the contradiction, so violations stay <= scanned.
    for message in inconsistencies:
        violations.append(message)

    row = coverage_row(len(numbers), excluded, min(bad, len(numbers) - excluded))
    finding = result(CHECK_PIN, row["scanned"], violations,
                     f"{row['scanned']} pin semantik olarak karşılaştırıldı "
                     f"(eligible={row['eligible']}, excluded={row['excluded']})")
    return finding, row, universe


def check_pad_identity(src_pads: List[dict], fp_pads: Dict[str, dict],
                       mechanical: List[dict], sym_pins: Dict[str, dict],
                       exposed_pad: Optional[dict],
                       *, footprint_error: Optional[str],
                       symbol_error: Optional[str],
                       exceptions: Dict[str, str]):
    """Per-pad number + type + the exposed-pad treatment.

    The EPAD is the reason this check is separate from the pin check. A
    thermal pad that the datasheet declares and the footprint omits is the
    variant substitution ``cad_assets`` names in its header, and it also has
    to exist ON THE SYMBOL SIDE - a footprint EP with no corresponding
    symbol pin cannot be tied to GND, which MASTER_RULEBOOK Faz 1 requires,
    and ERC will not complain because there is nothing to complain about.
    """
    epad_number = None
    epad_required = False
    epad_symbol_pin = None
    if isinstance(exposed_pad, dict):
        epad_required = bool(exposed_pad.get("present"))
        epad_number = str(exposed_pad.get("pad_number") or "").strip() or None
        epad_symbol_pin = str(exposed_pad.get("symbol_pin_number") or "").strip() or None

    numbers = sorted({str(p["number"]).strip() for p in src_pads}
                     | set(fp_pads), key=_sort_key)
    by_number = {str(p["number"]).strip(): p for p in src_pads}

    universe: List[dict] = []
    violations: List[str] = []
    bad = 0
    excluded = 0
    source_empty = not src_pads

    for number in numbers:
        declared = by_number.get(number)
        actual = fp_pads.get(number)
        entry = {
            "number": number,
            "type": (declared or {}).get("type"),
            "epad": bool(epad_number is not None and number == epad_number),
            "footprint_type": (actual or {}).get("type"),
            "footprint_shape": (actual or {}).get("shape"),
            "occurrences": (actual or {}).get("occurrences", 0),
        }
        if source_empty:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = (
                "otoriter kaynak pad tablosu boş - karşılaştırılacak kayıt yok")
            excluded += 1
        elif footprint_error or not fp_pads:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = (
                footprint_error or "aday footprint okunamadı / pad içermiyor")
            excluded += 1
        elif number in exceptions:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = exceptions[number]
            excluded += 1
        elif declared is None:
            entry["status"] = "missing_in_source"
            violations.append(
                f"pad {number}: footprint'te var, otoriter pad tablosunda YOK")
            bad += 1
        elif actual is None:
            entry["status"] = "missing_in_footprint"
            violations.append(
                f"pad {number}: otoriter tabloda var (tip={declared.get('type')!r}), "
                f"footprint'te YOK")
            bad += 1
        else:
            problems: List[str] = []
            want_type = declared.get("type")
            if want_type is not None and str(want_type).strip() != str(actual.get("type") or "").strip():
                problems.append(
                    f"pad tipi: beklenen {want_type!r}, footprint'te {actual.get('type')!r}")
            # The symbol-side half of the EPAD claim is only makeable when
            # the symbol was readable. Asserting it against a symbol nobody
            # could parse would report "the EP pin is missing" about a file
            # whose pins were never seen - a measured-sounding RED derived
            # from an absence of measurement.
            if (entry["epad"] and epad_symbol_pin and not symbol_error
                    and sym_pins and epad_symbol_pin not in sym_pins):
                problems.append(
                    f"exposed pad footprint'te var ama sembolde {epad_symbol_pin!r} "
                    "pini yok - GND'ye bağlanamaz (MASTER_RULEBOOK Faz 1)")
            if problems:
                entry["status"] = "mismatch"
                entry["mismatch"] = problems
                violations.append(f"pad {number}: " + "; ".join(problems))
                bad += 1
            else:
                entry["status"] = "matched"
        universe.append(entry)

    # The EPAD gets its own explicit statement, because "the number was not
    # in either table" is exactly how a missing thermal pad disappears.
    if epad_required and not source_empty and not footprint_error and fp_pads:
        if epad_number is None:
            violations.append(
                "exposed pad ZORUNLU ilan edildi ama kaynakta pad_number verilmemiş")
            bad += 1
        elif epad_number not in fp_pads:
            violations.append(
                f"exposed pad {epad_number!r} otoriter kaynakta ZORUNLU, "
                "footprint'te YOK - yanlış paket varyantı")
            bad += 1

    total = len(numbers)
    row = coverage_row(total, excluded, min(bad, max(total - excluded, 0)))
    finding = result(CHECK_PAD, row["scanned"], violations,
                     f"{row['scanned']} pad kimliği karşılaştırıldı; "
                     f"mekanik pad={len(mechanical)} (excluded)")
    return finding, row, universe


def check_padmap(sym_pins: Dict[str, dict], fp_pads: Dict[str, dict],
                 *, symbol_error: Optional[str], footprint_error: Optional[str],
                 source_readable: bool):
    """Symbol pin numbers and footprint pad numbers as SETS, not counts.

    ``cad_assets.check_pin_count`` already compares the two CARDINALITIES.
    Equal cardinality with disjoint membership is the pathological case it
    passes and this one does not: a five-pad footprint numbered 1..5 next to
    a five-pin symbol numbered A1..A5 is two counts of five.

    ``source_readable=False`` EXCLUDES the whole universe even though the
    comparison is technically computable from the two files alone. It is
    computable and it is not evidence: two files agreeing with each other
    says nothing about whether either is the right part, and letting it
    count as coverage would leave a run with no authoritative source
    reporting ``scanned > 0`` - a gate reporting that it examined something
    when the only thing it could examine was itself.
    """
    numbers = sorted(set(sym_pins) | set(fp_pads), key=_sort_key)
    violations: List[str] = []
    bad = 0
    excluded = 0
    blocked = (symbol_error or footprint_error or not sym_pins or not fp_pads
               or not source_readable)
    entries: List[dict] = []
    for number in numbers:
        entry = {"number": number,
                 "in_symbol": number in sym_pins,
                 "in_footprint": number in fp_pads}
        if blocked:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = (
                symbol_error or footprint_error
                or (None if source_readable else
                    "otoriter pin/paket kaynağı yok - sembol ile footprint'in "
                    "birbirine uyması ikisinin de DOĞRU parça olduğunu göstermez")
                or "sembol veya footprint pin/pad içermiyor")
            excluded += 1
        elif not entry["in_symbol"]:
            entry["status"] = "missing_in_symbol"
            violations.append(f"{number}: footprint'te pad var, sembolde pin yok")
            bad += 1
        elif not entry["in_footprint"]:
            entry["status"] = "missing_in_footprint"
            violations.append(f"{number}: sembolde pin var, footprint'te pad yok")
            bad += 1
        else:
            entry["status"] = "matched"
        entries.append(entry)

    row = coverage_row(len(numbers), excluded, bad)
    finding = result(CHECK_PADMAP, row["scanned"], violations,
                     f"sembol pin kümesi ∩ footprint pad kümesi: "
                     f"{row['scanned']} numara eşlendi")
    return finding, row, entries


def check_package_identity(source: dict, fp_pads: Dict[str, dict],
                           fp_name: Optional[str], epad_number: Optional[str],
                           *, footprint_error: Optional[str],
                           exceptions: Dict[str, str]):
    """Package variant identity: the label and the measurable geometry.

    Three attributes, each independently excludable WITH A REASON:

      ``footprint_name``  the candidate's own name must contain the token
                          the source declares. THIS PROVES LABELLING, NOT
                          GEOMETRY, and it is recorded as such - a file can
                          be named anything. It is still worth checking,
                          because the wrong-variant substitutions that reach
                          production are usually correctly labelled and
                          wrongly sourced, or the reverse, and disagreement
                          between the two is the signal.
      ``pad_span_x_mm``   measurable: the centre-to-centre span.
      ``pad_span_y_mm``   same, other axis.

    An attribute the source does not state is EXCLUDED, never defaulted,
    and an excluded attribute means package variant identity was not proven
    - which caps the run at CONFIRM. That is the honest answer: a land
    pattern table with no numbers in it cannot certify a footprint.
    """
    land = source.get("land_pattern") if isinstance(source.get("land_pattern"), dict) else {}
    tolerance = land.get("tolerance_mm")
    span_x, span_y, sampled = pad_centre_span(fp_pads, epad_number)

    attributes: List[dict] = []
    violations: List[str] = []
    bad = 0
    excluded = 0

    def add(name: str, declared, measured, ok_reason: Optional[str],
            exclusion: Optional[str]) -> None:
        nonlocal bad, excluded
        entry = {"attribute": name, "declared": declared, "measured": measured}
        if name in exceptions:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = exceptions[name]
            excluded += 1
        elif exclusion is not None:
            entry["status"] = "excluded"
            entry["exclusion_reason"] = exclusion
            excluded += 1
        elif ok_reason is None:
            entry["status"] = "matched"
        else:
            entry["status"] = "mismatch"
            entry["mismatch"] = ok_reason
            violations.append(f"{name}: {ok_reason}")
            bad += 1
        attributes.append(entry)

    expected_name = str(source.get("expected_footprint_name") or "").strip()
    if footprint_error:
        add("footprint_name", expected_name or None, None, None, footprint_error)
    elif not expected_name:
        add("footprint_name", None, fp_name, None,
            "kaynak expected_footprint_name bildirmiyor - paket etiketi "
            "doğrulanamaz (varsayılan üretilmez)")
    elif fp_name is None:
        add("footprint_name", expected_name, None,
            "aday footprint bir (footprint \"AD\" ...) düğümü taşımıyor", None)
    else:
        mismatch = (None if expected_name.lower() in fp_name.lower()
                    else f"beklenen belirteç {expected_name!r}, footprint adı {fp_name!r}")
        add("footprint_name", expected_name, fp_name, mismatch, None)

    for axis, declared_key, measured in (("pad_span_x_mm", "pad_span_x_mm", span_x),
                                         ("pad_span_y_mm", "pad_span_y_mm", span_y)):
        declared = land.get(declared_key)
        if footprint_error:
            add(axis, declared, None, None, footprint_error)
        elif not isinstance(declared, (int, float)) or isinstance(declared, bool):
            add(axis, None, measured, None,
                "otoriter land pattern bu ölçüyü bildirmiyor - "
                "varsayılan bir değer ÜRETİLMEZ")
        elif not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool):
            add(axis, declared, measured, None,
                "land_pattern.tolerance_mm bildirilmemiş - kaynaksız bir "
                "tolerans uydurmak yerine bu öznitelik excluded")
        elif measured is None:
            add(axis, declared, None, None,
                f"ölçülemedi: exposed pad dışında {sampled} pad merkezi var, "
                "aralık için en az 2 gerekli")
        else:
            delta = round(abs(measured - float(declared)), 4)
            mismatch = (None if delta <= float(tolerance) else
                        f"beklenen {declared} mm ±{tolerance}, ölçülen {measured} mm "
                        f"(fark {delta} mm)")
            add(axis, declared, measured, mismatch, None)

    row = coverage_row(len(attributes), excluded, bad)
    finding = result(CHECK_PACKAGE, row["scanned"], violations,
                     f"{row['scanned']}/{row['eligible']} paket kimlik özniteliği "
                     "otoriter kaynağa karşı ölçüldü")
    measured_block = {"footprint_name": fp_name, "pad_span_x_mm": span_x,
                      "pad_span_y_mm": span_y, "pad_centres_sampled": sampled}
    return finding, row, attributes, measured_block


def _blank_pad_artifact(symbol_text: str, footprint_text: str) -> bool:
    """Is the base gate's pad-count mismatch caused ONLY by numberless pads?

    MEASURED, 2026-08-10, against the real function - not a suspicion.
    ``cad_assets.footprint_pad_numbers`` returns ``sorted(set(pads))`` over
    every ``(pad "N" ...)`` form, and KiCad writes a mounting/mechanical pad
    as ``(pad "" np_thru_hole ...)``. So the empty string survives into the
    set, and ``check_pin_count`` reports:

        footprint_vs_sembol: 6 pad / 5 pin; sembolde olmayan pad=['']

    on a footprint that is perfectly correct and merely has one mounting
    hole. That is a false mismatch in the CARDINALITY gate, and inheriting
    it would make this tool report RED on a correct asset - the exact
    failure mode (a gate that cries wolf) that gets gates switched off.

    ``cad_assets.py`` is owned elsewhere and is not edited from here. The
    condition is instead DETECTED precisely: if dropping the numberless
    pads makes the two cardinalities agree, the mismatch is an artifact of
    the upstream reader and not a claim about this asset. It is recorded,
    the comparison is EXCLUDED with that reason, and the same claim is
    still measured - more strongly, as set equality - by
    ``sembol_footprint_padmap``, which excludes mechanical pads explicitly.
    """
    pads = set(footprint_pad_numbers(footprint_text))
    if "" not in pads:
        return False
    return len(pads - {""}) == len(set(symbol_pin_numbers(symbol_text)))


def check_base_count_gate(symbol_text: Optional[str], footprint_text: Optional[str],
                          datasheet_pins: int, *, source_pins_known: bool):
    """``cad_assets.check_pin_count`` re-run underneath, as ONE finding.

    Imported, not reimplemented: this tool is a layer on top of that gate,
    and a second copy of the cardinality rule would drift from the first.

    Its coverage universe is the TWO comparisons it makes, not the pin
    count. That is what the check actually examines, and stating the pin
    count as the denominator would break ``violations <= scanned`` on a
    one-pin part with two failed comparisons - a denominator from the wrong
    population is the failure ``by_check_map`` warns about.

    ``source_pins_known=False`` excludes BOTH comparisons. The
    ``datasheet_pin_count`` argument is derived from the authoritative pin
    table, and with no table that number is 0 - which would make
    ``check_pin_count`` report "symbol has 5 pins, datasheet has 0" and
    turn a missing source into RED. A source nobody could read is
    NO_COVERAGE; reporting it as a measured mismatch would be a fabricated
    measurement pointing the wrong way.
    """
    if not source_pins_known:
        return (result(CHECK_BASE, 0, [],
                       "otoriter pin tablosu yok - datasheet pin sayısı bilinmiyor"),
                coverage_row(2, 2, 0), None)
    if symbol_text is None or footprint_text is None:
        return (result(CHECK_BASE, 0, [], "sembol veya footprint okunamadı"),
                coverage_row(2, 2, 0), None)
    inner = check_pin_count(symbol_text, datasheet_pins, footprint_text)
    if inner["status"] == NO_COVERAGE:
        return (result(CHECK_BASE, 0, [], inner["detail"]),
                coverage_row(2, 2, 0), inner)

    violations: List[str] = []
    artifacts: List[str] = []
    excluded = 0
    blank_artifact = _blank_pad_artifact(symbol_text, footprint_text)
    for message in inner["violations"]:
        if message.startswith("footprint_vs_sembol") and blank_artifact:
            artifacts.append(message)
            excluded += 1
        else:
            violations.append(message)

    detail = inner["detail"]
    if artifacts:
        detail += ("; footprint_vs_sembol EXCLUDED: cad_assets numarasız mekanik "
                   "pad'i pad sayar - aynı iddia " + CHECK_PADMAP + " içinde "
                   "mekanik pad'ler dışlanarak ölçüldü")
    row = coverage_row(2, excluded, len(violations))
    finding = result(CHECK_BASE, row["scanned"], violations, detail)
    inner = dict(inner)
    inner["upstream_artifacts"] = artifacts
    return finding, row, inner


def lifecycle_for_mpn(data: Any, mpn: str) -> Tuple[Optional[str], bool]:
    """``(lifecycle, found)`` out of a lifecycle_report.json or parts list.

    Reading only. ``bom-lifecycle`` owns this field; this tool never writes
    it, never infers it and never upgrades a missing one to ``Active``.
    """
    lines: Any = []
    if isinstance(data, dict):
        for key in ("lines", "parts", "components"):
            if isinstance(data.get(key), list):
                lines = data[key]
                break
    elif isinstance(data, list):
        lines = data
    for line in lines:
        if isinstance(line, dict) and str(line.get("mpn") or "").strip() == mpn:
            return line.get("lifecycle"), True
    return None, False


def check_lifecycle_release(mpn: str, lifecycle: Optional[str], found: bool,
                            *, supplied: bool):
    """A refusal, not a selection.

    ``cad_assets.lifecycle_gate`` already encodes MASTER_RULEBOOK Bölüm 0's
    rule and it is called verbatim. The only thing added here is the scope:
    that gate refuses a DOWNLOAD, this one refuses a RELEASE.
    """
    if not supplied:
        return (result(CHECK_LIFECYCLE, 0, [],
                       "lifecycle_report.json / parts.json verilmedi"),
                coverage_row(1, 1, 0), None)
    if not found:
        return (result(CHECK_LIFECYCLE, 0, [],
                       f"{mpn}: lifecycle kaydında satır yok"),
                coverage_row(1, 1, 0), None)
    allowed, reason = lifecycle_gate(mpn, lifecycle)
    violations = [] if allowed else [reason]
    finding = result(CHECK_LIFECYCLE, 1, violations, reason)
    return finding, coverage_row(1, 0, len(violations)), reason


# --------------------------------------------------------------------------- #
# 5. Caps and the final verdict
# --------------------------------------------------------------------------- #
def land_pattern_cap(land: Any) -> Tuple[Optional[dict], dict]:
    """``(cap, land_pattern_block)``.

    The manufacturer's own land pattern is the only source that leaves a
    run eligible for GREEN. A derived one is not refused - it is recorded
    with the technique that derived it and pinned at LOCAL_POLICY, which is
    the coverage-gate's word for "chosen in this repo, not authoritative,
    cannot produce PASS". A fallback with no derivation recorded at all is
    weaker still: the files were compared, but nobody can say against what,
    so SCREEN_ONLY.
    """
    if not isinstance(land, dict):
        return ({"verdict": CONFIRM,
                 "reason": "land_pattern bloğu yok - üretici land pattern "
                           "bilgisi eksik/belirsiz"},
                {"source_type": None, "authoritative": False})
    source_type = str(land.get("source_type") or "").strip() or None
    block = {
        "source_type": source_type,
        "document_id": land.get("document_id"),
        "revision": land.get("revision"),
        "derivation": land.get("derivation"),
        "tolerance_mm": land.get("tolerance_mm"),
        "authoritative": source_type in LAND_PATTERN_AUTHORITATIVE,
    }
    if source_type in LAND_PATTERN_AUTHORITATIVE:
        gaps = [f for f in ("document_id", "revision") if not str(land.get(f) or "").strip()]
        if gaps:
            return ({"verdict": CONFIRM,
                     "reason": "üretici land pattern'i belirtilmiş ama "
                               + ", ".join(gaps) + " eksik"}, block)
        return None, block
    if source_type in LAND_PATTERN_DERIVED:
        if str(land.get("derivation") or "").strip():
            return ({"verdict": LOCAL_POLICY,
                     "reason": f"land pattern otoriter değil ({source_type}); "
                               f"türetme: {land.get('derivation')} - imza değildir"},
                    block)
        return ({"verdict": SCREEN_ONLY,
                 "reason": f"land pattern otoriter değil ({source_type}) ve "
                           "türetme tekniği kaydedilmemiş - yalnız tarama"},
                block)
    return ({"verdict": CONFIRM,
             "reason": f"land_pattern.source_type={source_type!r} - üretici "
                       "land pattern bilgisi eksik/belirsiz"}, block)


def strongest_cap(caps: Sequence[dict]) -> Optional[str]:
    for verdict in CAP_ORDER:
        if any(c["verdict"] == verdict for c in caps):
            return verdict
    return None


def final_verdict(findings: Sequence[dict], excluded_checks: Sequence[dict],
                  caps: Sequence[dict], stale_reasons: Sequence[str],
                  coverage: dict) -> Tuple[str, List[str]]:
    """The one place a verdict is decided. No verdict is invented here.

    Order, and why:

      1. INVALID_COVERAGE - broken arithmetic invalidates every number in
         the report, so nothing downstream may be read.
      2. RED - a MEASURED mismatch is never softened by a cap. A missing
         provenance field cannot make a wrong pin function less wrong.
      3. STALE - the files moved under the qualification; the measurement
         is about a state that no longer exists.
      4. NO_COVERAGE - nothing was examined. Never GREEN, never a cap:
         "we could not look" is not "we looked and it is unclear".
      5. the strongest cap.
      6. GREEN, which is reachable ONLY when every universe was scanned,
         nothing mismatched, and no cap applies.
    """
    reasons: List[str] = []
    broken = coverage_rule_violations(coverage)
    if broken:
        return INVALID_COVERAGE, [f"coverage aritmetiği bozuk: {', '.join(broken)}"]
    roll = rollup_status(findings, excluded={c["check"] for c in excluded_checks})
    if roll == FAIL:
        return RED, [f["check"] for f in findings if f["status"] == FAIL]
    if stale_reasons:
        return STALE, list(stale_reasons)
    if roll == NO_COVERAGE:
        reasons = [f["check"] for f in findings if f["status"] == NO_COVERAGE]
        return NO_COVERAGE, reasons
    cap = strongest_cap(caps)
    if cap is not None:
        return cap, [c["reason"] for c in caps if c["verdict"] == cap]
    return GREEN, []


# --------------------------------------------------------------------------- #
# 6. Qualification
# --------------------------------------------------------------------------- #
def qualify(*, source: Optional[dict], source_error: Optional[str],
            symbol_text: Optional[str], symbol_error: Optional[str],
            footprint_text: Optional[str], footprint_error: Optional[str],
            source_path: Optional[str] = None,
            symbol_path: Optional[str] = None,
            footprint_path: Optional[str] = None,
            lifecycle_data: Any = None, lifecycle_supplied: bool = False,
            lifecycle_path: Optional[str] = None,
            snapshot_path: Optional[str] = None,
            prior: Optional[dict] = None) -> dict:
    """Build the ``cad-asset-qualification-report/v1`` document."""
    if source is not None and not isinstance(source, dict):
        raise ContractError("pin/paket kaynağı bir JSON nesnesi olmalı")
    source = source or {}
    if source:
        require_dict(source, "pin_package_source")

    caps: List[dict] = []
    excluded_checks: List[dict] = []
    stale_reasons: List[str] = []

    mpn = str(source.get("mpn") or "").strip()
    package_code = str(source.get("package_code") or "").strip() or None

    if source_error:
        caps.append({"verdict": CONFIRM,
                     "reason": f"otoriter pin/paket kaynağı okunamadı: {source_error}"})
    missing_prov = provenance_missing(source) if source else list(PROVENANCE_FIELDS)
    if missing_prov:
        caps.append({"verdict": CONFIRM,
                     "reason": "AUTHORITATIVE_SOURCE provenansı eksik: "
                               + ", ".join(missing_prov)
                               + " (skills/_shared/coverage-gate.md)"})
    if not mpn:
        caps.append({"verdict": CONFIRM, "reason": "kaynak MPN bildirmiyor"})
    if not package_code:
        caps.append({"verdict": CONFIRM, "reason": "kaynak package_code bildirmiyor"})

    accepted, refused = accepted_exceptions(source)
    pin_exceptions = exception_index(accepted, "pin")
    pad_exceptions = exception_index(accepted, "pad")
    attr_exceptions = exception_index(accepted, "package_attribute")

    sym_pins: Dict[str, dict] = {}
    inconsistencies: List[str] = []
    if symbol_text is not None:
        sym_pins, inconsistencies = symbol_pins(symbol_text)
        if not sym_pins:
            symbol_error = symbol_error or "sembolde hiç pin bulunamadı"

    fp_pads: Dict[str, dict] = {}
    mechanical: List[dict] = []
    fp_name: Optional[str] = None
    if footprint_text is not None:
        fp_pads, mechanical = footprint_pads(footprint_text)
        fp_name = footprint_name(footprint_text)
        if not fp_pads:
            footprint_error = footprint_error or "footprint'te hiç pad bulunamadı"

    exposed = source.get("exposed_pad") if isinstance(source.get("exposed_pad"), dict) else None
    epad_number = (str(exposed.get("pad_number") or "").strip() or None) if exposed else None

    src_pins = source_pin_table(source)
    src_pads = source_pad_table(source)

    findings: List[dict] = []
    rows: Dict[str, Dict[str, int]] = {}

    pin_finding, pin_row, pin_universe = check_pin_identity(
        src_pins, sym_pins, symbol_error=symbol_error,
        exceptions=pin_exceptions, inconsistencies=inconsistencies)
    findings.append(pin_finding)
    rows[CHECK_PIN] = pin_row

    pad_finding, pad_row, pad_universe = check_pad_identity(
        src_pads, fp_pads, mechanical, sym_pins, exposed,
        footprint_error=footprint_error, symbol_error=symbol_error,
        exceptions=pad_exceptions)
    findings.append(pad_finding)
    rows[CHECK_PAD] = pad_row

    map_finding, map_row, map_entries = check_padmap(
        sym_pins, fp_pads, symbol_error=symbol_error,
        footprint_error=footprint_error,
        source_readable=bool(src_pins or src_pads))
    findings.append(map_finding)
    rows[CHECK_PADMAP] = map_row

    pkg_finding, pkg_row, pkg_attributes, pkg_measured = check_package_identity(
        source, fp_pads, fp_name, epad_number,
        footprint_error=footprint_error, exceptions=attr_exceptions)
    findings.append(pkg_finding)
    rows[CHECK_PACKAGE] = pkg_row

    base_finding, base_row, base_inner = check_base_count_gate(
        symbol_text, footprint_text, len(src_pins),
        source_pins_known=bool(src_pins))
    findings.append(base_finding)
    rows[CHECK_BASE] = base_row

    lifecycle_state, lifecycle_found = (None, False)
    if lifecycle_supplied:
        lifecycle_state, lifecycle_found = lifecycle_for_mpn(lifecycle_data, mpn)
    lc_finding, lc_row, lc_reason = check_lifecycle_release(
        mpn or "<MPN yok>", lifecycle_state, lifecycle_found,
        supplied=lifecycle_supplied)
    findings.append(lc_finding)
    rows[CHECK_LIFECYCLE] = lc_row

    # --- deliberate, recorded scope exclusions -------------------------- #
    # rollup_status skips an excluded NO_COVERAGE but NEVER an excluded
    # FAIL, so this cannot be used to talk a measured failure away.
    if lc_finding["status"] == NO_COVERAGE:
        reason = ("lifecycle kaydı verilmedi veya MPN o kayıtta yok - "
                  "sahibi bom-lifecycle; bu skill parça seçmez")
        excluded_checks.append({"check": CHECK_LIFECYCLE, "reason": reason})
        caps.append({"verdict": CONFIRM, "reason": reason})
    if pkg_finding["status"] == NO_COVERAGE and not footprint_error and src_pins:
        reason = ("otoriter kaynak hiçbir paket kimlik özniteliği bildirmiyor - "
                  "paket varyant kimliği KANITLANMADI")
        excluded_checks.append({"check": CHECK_PACKAGE, "reason": reason})
        caps.append({"verdict": CONFIRM, "reason": reason})
    for attribute in pkg_attributes:
        if attribute["status"] == "excluded" and attribute["attribute"] not in attr_exceptions:
            caps.append({"verdict": CONFIRM,
                         "reason": f"paket özniteliği {attribute['attribute']} "
                                   f"excluded: {attribute['exclusion_reason']}"})

    land_cap, land_block = land_pattern_cap(source.get("land_pattern"))
    if land_cap is not None:
        caps.append(land_cap)

    for entry in refused:
        caps.append({"verdict": CONFIRM,
                     "reason": f"istisna REDDEDİLDİ ({entry.get('scope')}/"
                               f"{entry.get('item')}): {entry['refusal_reason']}"})

    # --- hash bindings and staleness ------------------------------------ #
    assets = {"symbol": hashed(symbol_path), "footprint": hashed(footprint_path)}
    source_file = hashed(source_path)
    declared_snapshot = str((source.get("provenance") or {}).get("snapshot_sha256") or "").strip()
    snapshot = hashed(snapshot_path) if snapshot_path else {"path": None, "sha256": None}
    snapshot_verified: Optional[bool] = None
    if snapshot_path:
        snapshot_verified = bool(declared_snapshot) and snapshot["sha256"] == declared_snapshot
        if not snapshot_verified:
            stale_reasons.append(
                f"provenance.snapshot_sha256={declared_snapshot or '<yok>'} ile "
                f"{snapshot_path} özeti {snapshot['sha256']} uyuşmuyor - "
                "kaynak anlık görüntüsü değişmiş")

    if isinstance(prior, dict):
        prior_assets = prior.get("assets") or {}
        for role, current in (("symbol", assets["symbol"]),
                              ("footprint", assets["footprint"])):
            was = (prior_assets.get(role) or {}).get("sha256")
            if was and current["sha256"] and was != current["sha256"]:
                stale_reasons.append(
                    f"{role} özeti kalifikasyondan sonra değişti: {was} -> {current['sha256']}")
        was_source = (prior.get("source") or {}).get("file_sha256")
        if was_source and source_file["sha256"] and was_source != source_file["sha256"]:
            stale_reasons.append(
                f"otoriter kaynak dosyası değişti: {was_source} -> {source_file['sha256']}")

    coverage = sum_coverage(rows)
    coverage["per_metric"] = rows
    verdict, verdict_reasons = final_verdict(
        findings, excluded_checks, caps,
        stale_reasons, {k: coverage[k] for k in
                        ("eligible", "scanned", "excluded", "violations")})

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "mpn": mpn or None,
        "package_code": package_code,
        "package_family": source.get("package_family"),
        "manufacturer": source.get("manufacturer"),
        "source": {
            "path": source_file["path"],
            "file_sha256": source_file["sha256"],
            "schema_version": source.get("schema_version"),
            "provenance": source.get("provenance") if isinstance(
                source.get("provenance"), dict) else None,
            "provenance_complete": not missing_prov,
            "provenance_missing": missing_prov,
            "snapshot": {**snapshot, "declared_sha256": declared_snapshot or None,
                         "verified": snapshot_verified},
            "read_error": source_error,
        },
        "assets": {
            "symbol": {**assets["symbol"], "read_error": symbol_error,
                       "pin_count": len(sym_pins)},
            "footprint": {**assets["footprint"], "read_error": footprint_error,
                          "pad_count": len(fp_pads),
                          "mechanical_pad_count": len(mechanical)},
        },
        "pin_universe": pin_universe,
        "pad_universe": pad_universe,
        "padmap": map_entries,
        "package": {
            "package_code": package_code,
            "expected_footprint_name": source.get("expected_footprint_name"),
            "declared_dimensions": source.get("package_dimensions"),
            "measured": pkg_measured,
            "attributes": pkg_attributes,
        },
        "land_pattern": land_block,
        "lifecycle": {
            "mpn": mpn or None,
            "state": lifecycle_state,
            "found_in_record": lifecycle_found,
            "record_path": lifecycle_path,
            "reason": lc_reason,
        },
        "exceptions": accepted,
        "refused_exceptions": refused,
        "excluded_checks": excluded_checks,
        "base_gate": base_inner,
        "checks": findings,
        "by_check": by_check_map(findings),
        "metrics": {f["check"]: (None if f["status"] == NO_COVERAGE
                                 else len(f["violations"])) for f in findings},
        "coverage": coverage,
        "summary": summarise(findings),
        "caps": caps,
        "stale_reasons": stale_reasons,
        "gate": {
            "verdict": verdict,
            "verdict_reasons": verdict_reasons,
            "signoff": False,
            "assurance_loop": ["L1", "L2"],
            "evidence_origin": ["AUTHORITATIVE_SOURCE", "DESIGN_ARTIFACT",
                                "TOOL_REPORT"],
            "writes_board": False,
        },
    }
    return report


# --------------------------------------------------------------------------- #
# 7. Gate receipt
# --------------------------------------------------------------------------- #
_SEMANTICS = {
    CHECK_PIN: "Pin numarası + adı(işlevi) + elektriksel rolü otoriter tabloya bağlanmayan pin sayısı",
    CHECK_PAD: "Pad numarası + tipi + exposed-pad işlemi otoriter tabloya bağlanmayan pad sayısı",
    CHECK_PADMAP: "Sembol pin kümesi ile footprint pad kümesi arasında eşleşmeyen numara sayısı",
    CHECK_PACKAGE: "Otoriter kaynağa karşı uyuşmayan paket kimlik özniteliği sayısı",
    CHECK_BASE: "cad_assets.check_pin_count'un iki kardinalite karşılaştırmasından başarısız olanların sayısı",
    CHECK_LIFECYCLE: "Serbest bırakmayı engelleyen lifecycle durumu sayısı (0 veya 1)",
}


def build_receipt(report: dict, argv: Sequence[str], *, start_utc: str,
                  end_utc: str, exit_code: int,
                  inputs: Sequence[Tuple[str, dict]],
                  rules: Optional[dict] = None) -> dict:
    """A ``gate-receipt/v1`` document for this run.

    WHICH GATE-RECEIPT CONTRACT THIS FOLLOWS, AND WHY
    ---------------------------------------------------------------------
    The bundle carries TWO gate-receipt contracts and they DISAGREE
    (measured 2026-08-10):

      * ``skills/pcb-orchestrator/references/gate_receipt.schema.json``
        wants ``metric_semantics.<id>.metric_definition`` and is
        ``additionalProperties: false``;
      * ``tools/scripts/gate_receipt.py`` (``SEMANTIC_FIELDS``, line 56)
        REQUIRES ``definition`` and ``sampling_step`` and rejects a record
        without them - and ``tools/schemas/gate_receipt.schema.json`` says
        in its own description that "the Python validator remains
        authoritative".

    The two field sets cannot both be satisfied in one document. This
    receipt follows the VALIDATOR, because a receipt the validator rejects
    cannot open a gate, and because ``promote.py`` and
    ``tools/fixtures/gate_receipt/usb_pair_positive.json`` already emit
    that shape. The divergence is recorded in the SKILL.md open-question
    block rather than papered over here.

    Consequences of following the validator, all deliberate:

      * ``parsed_metrics`` and ``coverage.per_metric`` carry the SAME key
        set - ``validate_coverage`` requires per-metric ids to match the
        parsed metric leaves EXACTLY.
      * every leaf is an integer count. A ``null`` leaf is INVALID_METRIC,
        and a ``0`` leaf on an unmeasured metric would compare equal to a
        threshold of 0 and report a vacuous PASS. Unmeasured spans stay in
        the report and out of the receipt.
      * ``blockers`` are ``{code, message}`` OBJECTS, never strings, and
        the list is always present. A literal ``null`` crashes
        ``gate_receipt.py:932`` with a TypeError, and a GREEN receipt with
        any blocker is turned RED by ``validate_blockers`` - which is
        correct, and is why the GREEN path here produces none.
      * ``rules`` is the AUTHORITATIVE SOURCE RECORD. ``validate_config``
        demands a config/rules identity, and for this gate the pin/package
        record IS the rule set being enforced; pointing it anywhere else
        would be decoration.
      * paths are ABSOLUTE and hash-bound. ``resolve_path`` resolves a
        relative path against the RECEIPT's directory, not ``--root``, so
        an absolute path is the only one that means the same thing from
        wherever the receipt is validated. Run
        ``gate_receipt.py --root <dir>`` with a directory that contains
        both this tool and the artifacts.

    NOTE on ``verdict``: ``CONFIRM`` / ``LOCAL_POLICY`` / ``SCREEN_ONLY``
    are in the orchestrator schema's enum but not in the validator's
    ``GREEN_WORDS``/``RED_WORDS``, so ``effective_status`` falls through to
    ``RED`` for them. That is the conservative reading and the right one
    for a release gate: none of the three opens a door.
    """
    rows = report["coverage"]["per_metric"]
    parsed = {name: row["violations"] for name, row in rows.items()}
    semantics = {
        name: {
            "definition": _SEMANTICS.get(name, name),
            "units": "count",
            "distance_semantics": ("centerline" if name == CHECK_PACKAGE
                                   else "not_applicable"),
            "region": "tek MPN/paket için sembol+footprint çifti",
            "sampling_step": ("not_applicable: kesikli öğe sayımı, "
                              "örnekleme adımı yok"),
            "geometry_model": "not_applicable",
        }
        for name in rows
    }

    verdict = report["gate"]["verdict"]
    blockers = [{"code": f"CHECK_{name.upper()}", "message": name}
                for name in report["gate"]["verdict_reasons"]
                if isinstance(name, str) and name in rows]
    blockers += [{"code": "VERDICT_REASON", "message": reason}
                 for reason in report["gate"]["verdict_reasons"]
                 if not (isinstance(reason, str) and reason in rows)]
    blockers += [{"code": f"CAP_{cap['verdict']}", "message": cap["reason"]}
                 for cap in report["caps"]]

    coverage = {k: report["coverage"][k]
                for k in ("eligible", "scanned", "excluded", "violations")}
    coverage["per_metric"] = rows
    if coverage["scanned"] == 0:
        coverage["reason"] = "; ".join(report["gate"]["verdict_reasons"]) or \
            "hiçbir pin/pad/paket öğesi taranamadı"

    retain = (["receipt", "hashes", "metrics", "reproducibility_command"]
              if verdict == GREEN else
              ["receipt", "child_receipts", "logs", "scratch"])
    expires = end_utc
    try:
        parsed_end = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
        expires = (parsed_end + timedelta(days=365)).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass

    tool_path = Path(__file__).resolve()
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "gate_id": f"cad-asset-qualification:{report.get('mpn') or 'UNKNOWN_MPN'}",
        "gate_kind": "L1",
        "verdict": verdict,
        "tool": {
            "name": "cad-asset-qualification",
            "version": SCHEMA_VERSION,
            "path": str(tool_path),
            "sha256": sha256_file(tool_path),
        },
        "argv": list(argv) or [TOOL_NAME],
        "start_utc": start_utc,
        "end_utc": end_utc,
        "started_utc": start_utc,
        "finished_utc": end_utc,
        "exit_code": exit_code,
        "inputs": [{"role": role,
                    "path": str(Path(item["path"]).resolve()),
                    "sha256": item["sha256"]}
                   for role, item in inputs
                   if item.get("path") and item.get("sha256")],
        "parsed_metrics": parsed,
        "metric_semantics": semantics,
        "coverage": coverage,
        "blockers": blockers,
        "child_receipts": [],
        "retention": {"policy": "audit-window-365d", "expires_utc": expires,
                      "retain": retain},
    }
    if rules and rules.get("path") and rules.get("sha256"):
        receipt["rules"] = {"path": str(Path(rules["path"]).resolve()),
                            "sha256": rules["sha256"]}
    if not receipt["inputs"]:
        # minItems: 1. A receipt with no hash-bound input cannot tie the
        # verdict to anything, and saying so out loud is better than
        # emitting a receipt a validator rejects for a reason that looks
        # unrelated to the real problem.
        receipt["inputs"] = [{"role": "none", "path": "<hash-bound girdi yok>",
                              "sha256": "0" * 64}]
    return receipt


# --------------------------------------------------------------------------- #
# 8. CLI
# --------------------------------------------------------------------------- #
SAMPLE_SNAPSHOT = (
    "CONSTRUCTED FIXTURE - not a real datasheet. Stands in for the snapshot "
    "document that a pin_package_source record is transcribed from.\n"
)

SAMPLE_SYMBOL = """(kicad_symbol_lib
  (symbol "U_FIXTURE"
    (property "Datasheet" "(number \\"99\\")")
    (symbol "U_FIXTURE_1_1"
      (pin power_in line (at 0 0 0) (name "VIN") (number "1" (effects)))
      (pin passive line (at 0 0 0) (name "GND") (number "2"))
      (pin output line (at 0 0 0) (name "SW") (number "3"))
      (pin input line (at 0 0 0) (name "EN") (number "4"))
      (pin passive line (at 0 0 0) (name "EP") (number "EP")))
    (symbol "U_FIXTURE_1_2"
      (pin power_in line (at 0 0 0) (name "VIN") (number "1")))))
"""

SAMPLE_FOOTPRINT = """(footprint "FixtureLib:DFN-4-1EP_1.5x1.5mm_P1mm"
  (pad "1" smd roundrect (at -0.75 -0.5) (size 0.4 0.3))
  (pad "2" smd roundrect (at -0.75 0.5) (size 0.4 0.3))
  (pad "3" smd roundrect (at 0.75 0.5) (size 0.4 0.3))
  (pad "4" smd roundrect (at 0.75 -0.5) (size 0.4 0.3))
  (pad "EP" smd rect (at 0 0) (size 0.8 0.8))
  (pad "" np_thru_hole circle (at 2 2) (size 1 1))
)
"""

SAMPLE_SOURCE: Dict[str, Any] = {
    "schema_version": SOURCE_SCHEMA_VERSION,
    "mpn": "FIXTURE-DFN4EP",
    "manufacturer": "Fixture Semiconductor",
    "package_code": "DRL0004A",
    "package_family": "DFN-4-1EP",
    "expected_footprint_name": "DFN-4-1EP_1.5x1.5mm",
    "provenance": {
        "document_id": "FIXTURE-DS-0001",
        "revision": "B",
        "locator": "Table 6-1 (Pin Functions) + Section 11.1 (Package Outline)",
        "accessed": "2026-08-10",
        "snapshot_sha256": sha256_text(SAMPLE_SNAPSHOT),
    },
    "pins": [
        {"number": "1", "name": "VIN", "function": "input supply",
         "electrical_role": "power_in"},
        {"number": "2", "name": "GND", "function": "ground return",
         "electrical_role": "passive"},
        {"number": "3", "name": "SW", "function": "switch node",
         "electrical_role": "output"},
        {"number": "4", "name": "EN", "function": "enable",
         "electrical_role": "input"},
        {"number": "EP", "name": "EP", "function": "thermal pad, tie to GND",
         "electrical_role": "passive"},
    ],
    "pads": [
        {"number": "1", "type": "smd"},
        {"number": "2", "type": "smd"},
        {"number": "3", "type": "smd"},
        {"number": "4", "type": "smd"},
        {"number": "EP", "type": "smd"},
    ],
    "exposed_pad": {"present": True, "pad_number": "EP",
                    "symbol_pin_number": "EP", "must_be_tied_to": "GND"},
    "land_pattern": {
        "source_type": "manufacturer",
        "document_id": "FIXTURE-DS-0001",
        "revision": "B",
        "pad_span_x_mm": 1.5,
        "pad_span_y_mm": 1.0,
        "tolerance_mm": 0.05,
    },
    "package_dimensions": {"body_x_mm": 1.5, "body_y_mm": 1.5, "pitch_mm": 1.0},
}

SAMPLE_LIFECYCLE: Dict[str, Any] = {
    "schema_version": "lifecycle-report/v1",
    "lines": [{"refdes": ["U1"], "mpn": "FIXTURE-DFN4EP", "lifecycle": "Active"}],
}


def _now_iso(value: Optional[str] = None) -> str:
    if value:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _self_test() -> int:
    import copy
    import tempfile

    pins, bad = symbol_pins(SAMPLE_SYMBOL)
    assert sorted(pins) == ["1", "2", "3", "4", "EP"], sorted(pins)
    assert not bad, bad
    assert pins["1"]["electrical_role"] == "power_in"
    assert symbol_pin_numbers(SAMPLE_SYMBOL) == sorted(pins), "cad_assets ile aynı pin kümesi"

    pads, mech = footprint_pads(SAMPLE_FOOTPRINT)
    assert sorted(pads) == ["1", "2", "3", "4", "EP"], sorted(pads)
    assert len(mech) == 1, "numarasız mekanik pad ayrı sayılmalı"
    assert sorted(pads) == [n for n in footprint_pad_numbers(SAMPLE_FOOTPRINT) if n]
    assert pad_centre_span(pads, "EP")[:2] == (1.5, 1.0)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        symbol = root / "part.kicad_sym"
        symbol.write_text(SAMPLE_SYMBOL, encoding="utf-8")
        footprint = root / "part.kicad_mod"
        footprint.write_text(SAMPLE_FOOTPRINT, encoding="utf-8")
        source = root / "source.json"
        source.write_text(json.dumps(SAMPLE_SOURCE), encoding="utf-8")
        lifecycle = root / "lifecycle.json"
        lifecycle.write_text(json.dumps(SAMPLE_LIFECYCLE), encoding="utf-8")

        def run(src: Dict[str, Any]) -> dict:
            return qualify(
                source=src, source_error=None,
                symbol_text=SAMPLE_SYMBOL, symbol_error=None,
                footprint_text=SAMPLE_FOOTPRINT, footprint_error=None,
                source_path=str(source), symbol_path=str(symbol),
                footprint_path=str(footprint),
                lifecycle_data=SAMPLE_LIFECYCLE, lifecycle_supplied=True,
                lifecycle_path=str(lifecycle))

        good = run(SAMPLE_SOURCE)
        assert good["gate"]["verdict"] == GREEN, good["gate"]

        flipped = copy.deepcopy(SAMPLE_SOURCE)
        flipped["pins"][0]["name"] = "GND"
        flipped["pins"][1]["name"] = "VIN"
        red = run(flipped)
        assert red["gate"]["verdict"] == RED, red["gate"]
        assert len(flipped["pins"]) == len(SAMPLE_SOURCE["pins"]), "pin SAYISI aynı kaldı"
        assert red["by_check"][CHECK_BASE]["status"] == "PASS", (
            "sayım kapısı bu hatayı göremez - semantik kapının varlık sebebi budur")
        assert red["by_check"][CHECK_PIN]["status"] == "FAIL"

        no_ep = copy.deepcopy(SAMPLE_SOURCE)
        stripped = SAMPLE_FOOTPRINT.replace('  (pad "EP" smd rect (at 0 0) (size 0.8 0.8))\n', "")
        missing = qualify(source=no_ep, source_error=None,
                          symbol_text=SAMPLE_SYMBOL, symbol_error=None,
                          footprint_text=stripped, footprint_error=None)
        assert missing["gate"]["verdict"] == RED, missing["gate"]

        empty = qualify(source={}, source_error="kaynak yok",
                        symbol_text=SAMPLE_SYMBOL, symbol_error=None,
                        footprint_text=SAMPLE_FOOTPRINT, footprint_error=None)
        assert empty["gate"]["verdict"] == NO_COVERAGE, empty["gate"]
        assert empty["coverage"]["scanned"] == 0, empty["coverage"]
        assert empty["coverage"]["eligible"] > 0, "aday hâlâ eligible"

        derived = copy.deepcopy(SAMPLE_SOURCE)
        derived["land_pattern"]["source_type"] = "ipc_derived"
        derived["land_pattern"]["derivation"] = "IPC-7351B nominal, density level M"
        assert run(derived)["gate"]["verdict"] == LOCAL_POLICY

        unsourced = copy.deepcopy(SAMPLE_SOURCE)
        unsourced["land_pattern"] = {"source_type": "unknown"}
        assert run(unsourced)["gate"]["verdict"] == CONFIRM

        prior = json.loads(json.dumps(good))
        prior["assets"]["symbol"]["sha256"] = "0" * 64
        stale = qualify(source=SAMPLE_SOURCE, source_error=None,
                        symbol_text=SAMPLE_SYMBOL, symbol_error=None,
                        footprint_text=SAMPLE_FOOTPRINT, footprint_error=None,
                        source_path=str(source), symbol_path=str(symbol),
                        footprint_path=str(footprint),
                        lifecycle_data=SAMPLE_LIFECYCLE, lifecycle_supplied=True,
                        prior=prior)
        assert stale["gate"]["verdict"] == STALE, stale["gate"]

        for report in (good, red, missing, empty, stale):
            cov = report["coverage"]
            assert cov["scanned"] + cov["excluded"] == cov["eligible"], cov
            assert not coverage_rule_violations(
                {k: cov[k] for k in ("eligible", "scanned", "excluded", "violations")}), cov

        out = root / "qual.json"
        receipt_path = root / "receipt.json"
        code = main(["--source", str(source), "--symbol", str(symbol),
                     "--footprint", str(footprint), "--lifecycle", str(lifecycle),
                     "--json", str(out), "--receipt", str(receipt_path),
                     "--now", "2026-08-10T00:00:00Z"])
        assert code == 0, code
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["verdict"] == GREEN
        assert set(receipt["parsed_metrics"]) == set(receipt["metric_semantics"])
        assert set(receipt["parsed_metrics"]) == set(receipt["coverage"]["per_metric"])

    diag("SELF-TEST PASS: per-pin semantic binding, pad/EPAD identity, "
         "symbol<->footprint pad map, package variant identity, the "
         "count-gate underneath, verdict caps, staleness and coverage "
         "arithmetic all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="authoritative pin/package record (JSON)")
    ap.add_argument("--symbol", help="candidate .kicad_sym")
    ap.add_argument("--footprint", help="candidate .kicad_mod")
    ap.add_argument("--lifecycle", help="lifecycle_report.json / parts.json (READ ONLY)")
    ap.add_argument("--source-snapshot",
                    help="the snapshot document provenance.snapshot_sha256 refers to")
    ap.add_argument("--prior", help="a previous cad_asset_qualification.json, for STALE")
    ap.add_argument("--json", help="cad_asset_qualification.json")
    ap.add_argument("--receipt", help="cad_asset_gate_receipt.json")
    ap.add_argument("--register", action="store_true",
                    help="register the pair into the project library - GREEN only")
    ap.add_argument("--project")
    ap.add_argument("--lib-name", default="project_cad_assets")
    ap.add_argument("--now", help="ISO-8601 timestamp for the receipt (reproducibility)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    for name in ("source", "symbol", "footprint"):
        if not getattr(a, name):
            ap.error(f"--{name} is required unless --self-test is given")

    start = _now_iso(a.now)
    source, source_error = read_json_soft(a.source)
    symbol_text, symbol_error = read_text_soft(a.symbol)
    footprint_text, footprint_error = read_text_soft(a.footprint)
    lifecycle_data, lifecycle_error = (None, None)
    if a.lifecycle:
        lifecycle_data, lifecycle_error = read_json_soft(a.lifecycle)
    prior, _ = read_json_soft(a.prior) if a.prior else (None, None)

    report = qualify(
        source=source, source_error=source_error,
        symbol_text=symbol_text, symbol_error=symbol_error,
        footprint_text=footprint_text, footprint_error=footprint_error,
        source_path=a.source, symbol_path=a.symbol, footprint_path=a.footprint,
        lifecycle_data=lifecycle_data,
        lifecycle_supplied=bool(a.lifecycle) and lifecycle_error is None,
        lifecycle_path=a.lifecycle, snapshot_path=a.source_snapshot, prior=prior)

    verdict = report["gate"]["verdict"]
    exit_code = status_exit_code(verdict)

    if a.register:
        if not a.project:
            ap.error("--register requires --project")
        # The gate is the gate. register_assets raises unless it is told the
        # gate passed, and it is told the truth.
        report["registration"] = register_assets(
            a.project, a.lib_name, a.symbol, a.footprint,
            pin_check_passed=verdict == GREEN)

    write_report(report, a.json)
    if a.receipt:
        receipt = build_receipt(
            report, ["cad_asset_qualification.py"] + list(argv or sys.argv[1:]),
            start_utc=start, end_utc=_now_iso(a.now), exit_code=exit_code,
            inputs=[("authoritative_source", {"path": a.source,
                                              "sha256": report["source"]["file_sha256"]}),
                    ("symbol", report["assets"]["symbol"]),
                    ("footprint", report["assets"]["footprint"]),
                    ("snapshot", report["source"]["snapshot"])],
            rules={"path": a.source, "sha256": report["source"]["file_sha256"]})
        write_report(receipt, a.receipt)

    emit({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
          "mpn": report["mpn"], "package_code": report["package_code"],
          "verdict": verdict, "verdict_reasons": report["gate"]["verdict_reasons"],
          "coverage": {k: report["coverage"][k] for k in
                       ("eligible", "scanned", "excluded", "violations")},
          "summary": report["summary"],
          "caps": [c["reason"] for c in report["caps"]]})
    return exit_code


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
