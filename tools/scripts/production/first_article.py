#!/usr/bin/env python3
"""First-article acceptance: does the board that was BUILT match the package
that was RELEASED?

backend: file  (pure stdlib)

WHY THIS TOOL EXISTS
--------------------
``cpl-assembly`` can be GREEN on every check it owns while the physical board
is wrong. Its own "cannot prove" table (skills/cpl-assembly/SKILL.md:99-113)
lists five rows it hands to somebody else - real mounting orientation, solder
void ratio, the fab accepting the package, MSL floor-life compliance, panel
stress - and then states at 109-113 that "serial production does not open
before this". It published no artifact that could carry that acceptance, so
the sentence had no consumer: the release decision was a claim in prose. This
tool is the missing consumer.

The failure has a specific shape, and it is invisible to every file-side
check. The CPL parses. The rotation map is versioned and side-aware. DNP
parity holds. The gerbers zip. Then one QFN comes back rotated 90 degrees
because the fab's zero-orientation convention changed for that package family;
or a BGA has a 40% void under a ball nobody can see; or the reels sat open past
their floor life and every plastic package popcorned in reflow; or the
assembler built from last week's zip. None of those are in the released files
and all of them are in the assembler's return evidence.

So the gate is NOT "were the files produced?" - ``production-outputs`` already
answers that - but "does the board that was actually built conform to the
released package?" The authority that follows is narrow and total: serial build
release or hold. ``cpl-assembly`` produces the package; ``dfm`` judges the
design's manufacturability; neither of them is the as-built release authority,
and neither of them can be, because both run before the board exists.

WHAT THIS TOOL DELIBERATELY DOES NOT DO
---------------------------------------
1. IT DOES NOT LOOK AT IMAGES. This is the hard rule of the whole design. The
   tool consumes a STRUCTURED vendor/machine/human result plus a hash-bound
   locator for the artefact that backs it. It never opens the locator, never
   decodes a pixel, and never forms an opinion about a photograph or an X-ray
   slice. "The model looked at the AOI image and it seemed fine" is the exact
   fabricated-evidence path this gate exists to close; an acceptance nobody
   can re-derive is not an acceptance. A PHOTO or MANUAL record with no
   ``signoff_standard`` therefore yields SCREEN_ONLY - an observation was
   made, no criterion was applied - rather than a pass.
2. IT DOES NOT JUDGE RELIABILITY. Solder metallurgy, intermetallic growth,
   thermal cycling and long-term joint life cannot be derived from a
   first-article inspection of one unit. GREEN here means "this build
   conforms to the released package", not "this design is qualified". Those
   are different claims with different evidence and confusing them would turn
   an assembly check into a reliability certificate nobody measured.
3. IT DOES NOT EDIT THE CPL. ``cpl-assembly`` owns the released package, the
   pick points, the fab rotation map and DNP/rotation parity. When this gate
   finds a rotation mismatch, the output is a RED verdict plus an L2 feedback
   edge naming ``cpl-assembly`` - not a rewritten rotation map. A gate that
   repairs its own input has stopped being a gate.
4. IT DOES NOT WRITE A DFM THRESHOLD. ``dfm`` owns process and design rules;
   ``xray_records[].acceptance_standard`` names the standard the VENDOR judged
   against. This tool records which standard was applied and whether the
   vendor's own verdict was PASS. It does not invent a void percentage.
5. IT DOES NOT OWN MSL/LIFECYCLE METADATA. ``bom-lifecycle`` is the canonical
   owner of ``msl_level`` and floor life. This tool checks the assembler's
   ACTUAL handling record AGAINST that metadata, which is the one thing
   ``bom-lifecycle`` cannot do: it has no visibility of what happened on the
   assembly floor.
6. IT IS NOT THE PRE-FAB DESIGN CHECKER. ``design-checker`` reads design
   artifacts before fabrication. This one reads the built board's return
   evidence afterwards. Same refdes universe, opposite side of the fab.

COVERAGE, AND THE ONE PLACE THIS TOOL IS OPINIONATED
----------------------------------------------------
``scanned + excluded == eligible`` is mandatory in every coverage block this
tool emits. That creates a question the other tools do not face: what happens
to a refdes in the released CPL that has neither an inspection record nor an
exclusion? It cannot be dropped (the sum would break) and it must not be
counted as scanned (nothing examined it).

It is folded into ``excluded`` with the literal reason
"no inspection record and no exclusion was supplied", listed by name in
``coverage_ledger.unaccounted_folded_into_excluded``, AND raised as a
violation in the presence check. The fold keeps the arithmetic honest - every
eligible item is accounted for, in a named category, with a stated reason -
and the simultaneous violation makes it impossible for the fold to read as a
benign decision. An unaccounted part is never quiet: it is in the ledger and
it is in the blockers.

Usage:
    first_article.py <fai_evidence.json> [--json report.json]
                     [--receipt-out assembly_acceptance_receipt.json]
                     [--deviation-log-out manufacturing_deviation_log.json]
                     [--verify-package PATH] [--rotation-tolerance-deg 1.0]
                     [--declare-out-of-scope REASON]
    first_article.py --self-test

Exit: 0 only for GREEN, 1 for RED / NO_COVERAGE / CONFIRM / SCREEN_ONLY /
STALE (the gate ran and the answer is "serial build does not open"), 2 when
the tool could not produce an answer at all.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    by_check_map,
    coverage_rule_violations,
    result,
    summarise,
)
from common.hashing import sha256_file, valid_digest  # noqa: E402
from common.report_io import (  # noqa: E402
    diag,
    emit,
    run_cli,
    status_exit_code,
    write_report,
)

SCHEMA_VERSION = "first-article-report/v1"
EVIDENCE_SCHEMA_VERSION = "fai-evidence/v1"
RECEIPT_SCHEMA_VERSION = "gate-receipt/v1"
TOOL_NAME = "first_article"

# The closed verdict vocabulary this gate is allowed to publish. Every one of
# these is a value skills/_shared/coverage-gate.md already defines; NO new
# verdict is introduced here. Only GREEN opens serial production.
GREEN = "GREEN"
RED = "RED"
CONFIRM = "CONFIRM"
SCREEN_ONLY = "SCREEN_ONLY"
STALE = "STALE"
# Exported so the report schema's enum and this module cannot drift apart
# without a test noticing - see test_first_article.ReportContract.
VERDICTS = (STALE, NO_COVERAGE, RED, CONFIRM, SCREEN_ONLY, GREEN)

# Verdict precedence, copied deliberately from gate_receipt.effective_status
# so the tool and the validator that closes the gate cannot disagree about
# which blocker gets named first. Note what the order does NOT mean: all five
# non-GREEN values block serial release equally. The order only decides which
# reason is reported first when several apply at once.
VERDICT_PRECEDENCE = (STALE, NO_COVERAGE, RED, CONFIRM, SCREEN_ONLY)

# PHOTO and MANUAL carry an observation, not a decision, unless the record
# names the criterion the observer judged against.
INTERPRETIVE_EVIDENCE = {"PHOTO", "MANUAL"}
EVIDENCE_TYPES = {"AOI", "XRAY", "PHOTO", "MANUAL", "ICT", "VENDOR_REPORT",
                  "ASSEMBLER_RECORD"}
EVIDENCE_REQUIRED_FIELDS = ("id", "type", "locator", "sha256", "recorded_utc")

# MSL 1 has unlimited floor life at <=30 C/60% RH, so it carries no handling
# obligation. Everything else does. The LEVELS are bom-lifecycle's; this set
# only records which of them put a refdes into the handling-record population.
MSL_UNLIMITED = {"1"}

UNACCOUNTED_REASON = "no inspection record and no exclusion was supplied"


class EvidenceError(ValueError):
    """The evidence document could not be read as a first-article record."""


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _norm(value) -> str | None:
    """Case/whitespace-insensitive comparison key. None stays None."""
    if value is None:
        return None
    text = str(value).strip()
    return text.casefold() if text else None


def angle_delta(measured, expected) -> float:
    """Absolute angular difference, taking the shorter way round.

    Same convention as ``production/assembly.py::check_orientation`` on
    purpose: the pre-fab check and the as-built check must not disagree about
    what "180 degrees out" means.
    """
    delta = abs(float(measured) - float(expected)) % 360.0
    return min(delta, 360.0 - delta)


def evidence_usable(record) -> tuple[bool, str]:
    """Is this evidence block a citable record, or just an attachment?

    Returns (usable, reason). A block missing its id/type/locator/digest/time
    cannot be re-derived by anyone, so the result it carries is not evidence.
    This is the function that stops "a model looked at the picture" from
    entering the gate: there is no code path where a locator alone accepts a
    part.
    """
    if not isinstance(record, dict):
        return False, "evidence block is missing"
    missing = [f for f in EVIDENCE_REQUIRED_FIELDS if not record.get(f)]
    if missing:
        return False, f"evidence record is missing {', '.join(missing)}"
    if record.get("type") not in EVIDENCE_TYPES:
        return False, f"unknown evidence type {record.get('type')!r}"
    if not valid_digest(record.get("sha256")):
        return False, "evidence sha256 is not a 64-char hex digest"
    return True, ""


def evidence_is_screen_only(record) -> bool:
    """PHOTO/MANUAL with no named signoff standard: observed, not judged."""
    return (isinstance(record, dict)
            and record.get("type") in INTERPRETIVE_EVIDENCE
            and not str(record.get("signoff_standard") or "").strip())


def coverage_block(eligible_ids, scanned_ids, excluded_entries,
                   violation_ids, reason: str = UNACCOUNTED_REASON):
    """One canonical ``eligible/scanned/excluded/violations`` triple.

    Returns ``(block, unaccounted)``. ``unaccounted`` are the eligible ids
    that were neither scanned nor explicitly excluded; they are folded into
    ``excluded`` (with ``reason``) so the arithmetic closes, and the caller is
    REQUIRED to raise them as a violation as well - see the module docstring.
    Folding without raising would be exactly the silent pass this gate exists
    to prevent, so this function returns the list rather than swallowing it.

    ``violations`` counts SCANNED ids only. A count that could exceed
    ``scanned`` would break ``violations <= scanned`` and be rejected as
    INVALID_COVERAGE by gate_receipt; unaccounted ids are blockers, not
    violations-of-a-thing-that-was-measured.

    ``reason`` is carried on every returned unaccounted entry rather than
    being a comment here, because the report schema requires every excluded
    entry to state why - including the ones that were excluded by this fold
    rather than by a person.
    """
    eligible = list(dict.fromkeys(eligible_ids))
    scanned = [r for r in dict.fromkeys(scanned_ids) if r in set(eligible)]
    explicit = [e for e in excluded_entries if e["refdes"] in set(eligible)]
    accounted = set(scanned) | {e["refdes"] for e in explicit}
    unaccounted = [{"refdes": r, "reason": reason}
                   for r in eligible if r not in accounted]
    block = {
        "eligible": len(eligible),
        "scanned": len(scanned),
        "excluded": len(explicit) + len(unaccounted),
        "violations": len([r for r in violation_ids if r in set(scanned)]),
    }
    broken = coverage_rule_violations(block)
    if broken:                                  # pragma: no cover - guard
        raise EvidenceError(
            f"internally inconsistent coverage block {block}: {broken}. This "
            "is a bug in first_article, not in the evidence; a gate that "
            "publishes broken coverage arithmetic must not run.")
    return block, unaccounted


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_evidence(path) -> dict:
    """Read and shape-check the FAI evidence document.

    An unrecognised ``schema_version`` RAISES rather than being read
    best-effort. The reader would not know which fields it was missing, and a
    gate that cannot read its input must not report on it - the same
    fail-closed rule ``production/contracts.py`` applies to a DRC contract
    from a future writer.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError("evidence root must be a JSON object")
    version = data.get("schema_version")
    if version != EVIDENCE_SCHEMA_VERSION:
        raise EvidenceError(
            f"unsupported evidence schema {version!r}; this tool reads "
            f"{EVIDENCE_SCHEMA_VERSION}. Fail-closed: an unreadable record "
            "cannot accept a build.")
    for key in ("identity", "released_package"):
        if not isinstance(data.get(key), dict):
            raise EvidenceError(f"${key} object is required")
    if not isinstance(data.get("refdes_universe"), list):
        raise EvidenceError(
            "$refdes_universe array is required - it is the denominator of "
            "every coverage triple and cannot be derived from the evidence "
            "itself without the evidence becoming its own denominator")
    return data


# --------------------------------------------------------------------------- #
# Check 1 - the released package binding
# --------------------------------------------------------------------------- #
def check_released_package(evidence: dict, verify_path=None) -> tuple[Finding, str]:
    """Is this evidence about the package that was actually released?

    Returns ``(finding, binding_state)`` where binding_state is one of
    BOUND / HASH_MISMATCH / REVISION_MISMATCH / UNBOUND. The two mismatch
    states are deliberately different verdicts:

      * REVISION_MISMATCH -> STALE. The evidence is about a different board
        revision. It may be perfectly good evidence; it is evidence of
        something else.
      * HASH_MISMATCH -> RED. Same declared revision, different bytes. That is
        a silent substitution: somebody built from a package that is not the
        one on record under that name, and nothing downstream would notice.
        Staleness is a timing problem, this is an identity problem, and the
        identity problem is the more dangerous of the two.
    """
    identity = evidence.get("identity", {})
    released = evidence.get("released_package", {})
    built_rev = identity.get("board_revision")
    released_rev = released.get("board_revision")
    built_hash = identity.get("released_package_sha256")
    released_hash = released.get("sha256")

    violations: list[dict] = []
    state = "BOUND"

    if not built_hash or not released_hash:
        return (Finding(
            "released_package_binding", NO_COVERAGE, 0, [],
            "the assembler did not state which package digest it built from "
            "(identity.released_package_sha256) or the release side carries "
            "no digest; the built-vs-released identity is UNBOUND and was "
            "NOT assumed to match"), "UNBOUND")

    if _norm(built_rev) != _norm(released_rev):
        state = "REVISION_MISMATCH"
        violations.append({
            "problem": "REVISION_MISMATCH",
            "built_from_revision": built_rev,
            "released_revision": released_rev,
            "message": "the first article was built from a different board "
                       "revision than the released package; this evidence is "
                       "STALE for the current release",
        })
    elif built_hash != released_hash:
        state = "HASH_MISMATCH"
        violations.append({
            "problem": "RELEASED_PACKAGE_HASH_MISMATCH",
            "built_from_sha256": built_hash,
            "released_sha256": released_hash,
            "message": "same declared revision, different package bytes. The "
                       "unit was built from a package that is not the one on "
                       "record under this revision. Serial build is blocked.",
        })

    checks = 2
    if verify_path is not None:
        checks += 1
        actual = sha256_file(verify_path)
        if actual != released_hash:
            state = "HASH_MISMATCH" if state == "BOUND" else state
            violations.append({
                "problem": "RELEASED_PACKAGE_ON_DISK_MISMATCH",
                "path": str(verify_path),
                "on_disk_sha256": actual,
                "released_sha256": released_hash,
                "message": "the package file on disk does not hash to the "
                           "digest the release record claims",
            })
    return (result("released_package_binding", checks, violations,
                   "built-from digest and revision against the released "
                   "package identity"), state)


def check_evidence_revision(evidence: dict) -> Finding:
    """Every evidence record that names a board revision must name THIS one.

    A single AOI export carried over from the previous spin is enough to make
    a whole acceptance describe the wrong board, and it is the kind of thing
    nobody notices because the refdes all still exist.
    """
    identity_rev = _norm(evidence.get("identity", {}).get("board_revision"))
    records = list(_all_evidence_records(evidence))
    violations = [
        {"evidence_id": rec.get("id"), "problem": "EVIDENCE_REVISION_MISMATCH",
         "evidence_revision": rec.get("board_revision"),
         "unit_revision": evidence.get("identity", {}).get("board_revision")}
        for rec in records
        if rec.get("board_revision")
        and _norm(rec.get("board_revision")) != identity_rev
    ]
    return result("evidence_revision_binding", len(records), violations,
                  "every evidence record that declares a board revision "
                  "declares the revision of the unit under acceptance")


def _all_evidence_records(evidence: dict):
    for row in evidence.get("as_built", []) or []:
        if isinstance(row.get("evidence"), dict):
            yield row["evidence"]
    for key in ("xray_records", "msl_records"):
        for row in evidence.get(key, []) or []:
            if isinstance(row.get("evidence"), dict):
                yield row["evidence"]
    for key in ("depanel_record", "fab_dfm_acceptance"):
        block = evidence.get(key)
        if isinstance(block, dict) and isinstance(block.get("evidence"), dict):
            yield block["evidence"]


# --------------------------------------------------------------------------- #
# Check 2 - as-built conformance over the refdes universe
# --------------------------------------------------------------------------- #
def partition_universe(evidence: dict):
    """Split the refdes universe into scanned / excluded / unaccounted.

    ``scanned`` requires a USABLE evidence block, not merely a row: a row
    whose evidence has no digest or no timestamp cannot be re-checked by
    anybody, so it did not scan anything.
    """
    universe = {row["refdes"]: row for row in evidence.get("refdes_universe", [])
                if isinstance(row, dict) and row.get("refdes")}
    excluded = [
        {"refdes": e["refdes"], "reason": e["reason"],
         "approver": e.get("approver")}
        for e in evidence.get("excluded", []) or []
        if isinstance(e, dict) and e.get("refdes") and e.get("reason")
    ]
    excluded_ids = {e["refdes"] for e in excluded}
    records: dict[str, dict] = {}
    unusable: list[dict] = []
    for row in evidence.get("as_built", []) or []:
        if not isinstance(row, dict) or not row.get("refdes"):
            continue
        refdes = row["refdes"]
        if refdes in excluded_ids or refdes not in universe:
            continue
        usable, reason = evidence_usable(row.get("evidence"))
        if not usable:
            unusable.append({"refdes": refdes, "problem": "UNUSABLE_EVIDENCE",
                             "message": reason})
            continue
        records[refdes] = row
    return universe, records, excluded, unusable


def check_presence(universe, records, unaccounted, unusable) -> Finding:
    """Fitted-vs-not, plus the accounting for every refdes nobody looked at."""
    violations: list[dict] = list(unusable)
    scanned = 0
    for refdes, row in sorted(records.items()):
        expected_dnp = bool(universe[refdes].get("dnp"))
        present = row.get("present")
        if present is None:
            continue                       # unmeasured, handled by coverage
        scanned += 1
        if expected_dnp and present:
            violations.append({
                "refdes": refdes, "problem": "DNP_PART_FITTED",
                "message": "the released package marks this refdes DNP and "
                           "the assembler fitted it"})
        elif not expected_dnp and not present:
            violations.append({
                "refdes": refdes, "problem": "MISSING_PART",
                "message": "a populated refdes is absent from the built unit"})
    for entry in unaccounted:
        violations.append({
            "refdes": entry["refdes"], "problem": "UNACCOUNTED_REFDES",
            "message": entry["reason"] + "; it was folded into `excluded` "
                       "so the coverage arithmetic closes and raised here so "
                       "the fold cannot read as a decision"})
    if scanned == 0 and violations:
        # Nothing was measured AND something is wrong with the accounting.
        # Finding forbids a scanned==0 FAIL, and rightly: the honest report is
        # "this examined nothing", with the violations carried in the detail.
        return Finding("as_built_presence", NO_COVERAGE, 0, [],
                       f"no usable presence record; {len(violations)} "
                       f"accounting problem(s): "
                       + "; ".join(sorted({v['problem'] for v in violations})))
    return result("as_built_presence", scanned, violations,
                  "fitted/not-fitted against the released DNP marking, plus "
                  "the accounting of every eligible refdes")


def check_attribute(universe, records, attribute: str, check_name: str) -> Finding:
    """``value`` or ``package`` as-built against the released package."""
    violations: list[dict] = []
    scanned = 0
    for refdes, row in sorted(records.items()):
        expected = universe[refdes].get(attribute)
        actual = row.get(attribute)
        if expected is None or actual is None:
            continue                       # unmeasured on one side or both
        scanned += 1
        if _norm(expected) != _norm(actual):
            violations.append({
                "refdes": refdes, "problem": f"{attribute.upper()}_MISMATCH",
                "released": expected, "as_built": actual})
    return result(check_name, scanned, violations,
                  f"as-built {attribute} against the released package")


def check_orientation(universe, records, tolerance_deg: float = 1.0) -> Finding:
    """As-built rotation against the released CPL angle.

    The released CPL angle already carries cpl-assembly's fab rotation offset,
    so this compares like with like. A part outside tolerance is the single
    defect this whole gate was built around: it passes DRC, it passes the CPL
    format check, and the board is dead.
    """
    violations: list[dict] = []
    scanned = 0
    for refdes, row in sorted(records.items()):
        expected = universe[refdes].get("expected_rotation_deg")
        actual = row.get("rotation_deg")
        if expected is None or actual is None:
            continue
        scanned += 1
        delta = angle_delta(actual, expected)
        if delta > tolerance_deg:
            violations.append({
                "refdes": refdes, "problem": "AS_BUILT_ORIENTATION_MISMATCH",
                "polar": bool(universe[refdes].get("polar")),
                "released_deg": expected, "as_built_deg": actual,
                "delta_deg": round(delta, 3),
                "tolerance_deg": tolerance_deg,
                "feedback_target": "cpl-assembly"})
    return result("as_built_orientation", scanned, violations,
                  f"as-built angles within +/-{tolerance_deg} degrees of the "
                  f"released CPL angle")


# --------------------------------------------------------------------------- #
# Check 3 - the classes cpl-assembly explicitly could not close
# --------------------------------------------------------------------------- #
def check_hidden_joints(universe, excluded_ids,
                        xray_records) -> tuple[Finding, dict, list, list]:
    """X-ray over the hidden-joint population (BGA/LGA/QFN/bottom-terminated).

    Optical evidence does not reach these joints at all, so an AOI PASS on a
    BGA says nothing about the balls under it. With no X-ray the hidden-joint
    defect universe does not close, and the honest answer is NO_COVERAGE - not
    a GREEN inherited from the optical check that could not see the defect.
    """
    eligible = [r for r, row in sorted(universe.items())
                if row.get("hidden_joint") and r not in excluded_ids]
    by_refdes: dict[str, dict] = {}
    violations: list[dict] = []
    interpretive: list[str] = []
    for record in xray_records or []:
        if not isinstance(record, dict) or record.get("refdes") not in set(eligible):
            continue
        usable, reason = evidence_usable(record.get("evidence"))
        if not usable:
            violations.append({"refdes": record["refdes"],
                               "problem": "UNUSABLE_XRAY_EVIDENCE",
                               "message": reason})
            continue
        by_refdes[record["refdes"]] = record
        if record.get("result") == "FAIL":
            violations.append({
                "refdes": record["refdes"], "problem": "HIDDEN_JOINT_REJECTED",
                "void_pct": record.get("void_pct"),
                "acceptance_standard": record.get("acceptance_standard"),
                "message": "the vendor's own X-ray verdict is FAIL; this gate "
                           "reports it and does not re-judge the void number"})
        elif record.get("result") == "REQUIRES_INTERPRETATION":
            interpretive.append(record["refdes"])
        elif not str(record.get("acceptance_standard") or "").strip():
            interpretive.append(record["refdes"])
    block, unaccounted = coverage_block(
        eligible, list(by_refdes), [],
        [v["refdes"] for v in violations],
        reason="no X-ray record for a hidden-joint package")
    finding = result(
        "hidden_joint_xray", len(by_refdes), violations,
        f"{len(eligible)} hidden-joint refdes eligible; X-ray is the only "
        f"evidence class that reaches them")
    return finding, block, sorted(interpretive), unaccounted


def check_msl_handling(universe, excluded_ids,
                       msl_records) -> tuple[Finding, dict, list, list]:
    """The assembler's ACTUAL moisture handling against bom-lifecycle's level.

    bom-lifecycle owns ``msl_level``; it cannot see the assembly floor. This
    check is the other half: was the declared level actually honoured, and if
    floor life was exceeded, was a bake performed? An exceeded floor life with
    no bake is scrap, not a warning.
    """
    eligible = [r for r, row in sorted(universe.items())
                if row.get("msl_level")
                and row["msl_level"] not in MSL_UNLIMITED
                and row["msl_level"] != "unknown"
                and r not in excluded_ids]
    unknown_level = [r for r, row in sorted(universe.items())
                     if row.get("msl_level") == "unknown" and r not in excluded_ids]
    by_refdes: dict[str, dict] = {}
    violations: list[dict] = []
    for record in msl_records or []:
        if not isinstance(record, dict) or record.get("refdes") not in set(eligible):
            continue
        usable, reason = evidence_usable(record.get("evidence"))
        if not usable:
            violations.append({"refdes": record["refdes"],
                               "problem": "UNUSABLE_MSL_EVIDENCE",
                               "message": reason})
            continue
        refdes = record["refdes"]
        by_refdes[refdes] = record
        declared = universe[refdes].get("msl_level")
        if _norm(record.get("msl_level")) != _norm(declared):
            violations.append({
                "refdes": refdes, "problem": "MSL_LEVEL_DISAGREEMENT",
                "bom_lifecycle_level": declared,
                "assembler_level": record.get("msl_level"),
                "feedback_target": "bom-lifecycle",
                "message": "the assembler handled the part at a different MSL "
                           "level than the lifecycle record declares; this "
                           "gate does not decide which is right, it reports "
                           "that they disagree"})
        used = record.get("floor_life_hours_used")
        allowed = record.get("floor_life_hours_declared")
        if used is not None and allowed is not None and used > allowed \
                and not record.get("bake_performed"):
            violations.append({
                "refdes": refdes, "problem": "FLOOR_LIFE_EXCEEDED_NO_BAKE",
                "floor_life_hours_used": used,
                "floor_life_hours_declared": allowed,
                "message": "floor life exceeded and no bake was recorded"})
        if record.get("dry_pack_intact_on_arrival") is False \
                and not record.get("bake_performed"):
            violations.append({
                "refdes": refdes, "problem": "DRY_PACK_BREACHED_NO_BAKE",
                "message": "the dry pack was not intact on arrival and no "
                           "bake was recorded"})
    block, unaccounted = coverage_block(
        eligible, list(by_refdes), [],
        [v["refdes"] for v in violations],
        reason="no MSL handling record for a moisture-sensitive package")
    finding = result(
        "msl_handling_record", len(by_refdes), violations,
        f"{len(eligible)} moisture-sensitive refdes eligible; levels are "
        f"bom-lifecycle's, the handling record is the assembler's")
    return finding, block, sorted(unknown_level), unaccounted


def check_depanel(evidence: dict) -> tuple[Finding, list]:
    """The depanel trial. Panel stress reaches parts DRC never looks at.

    ``panelised: null`` is NOT "no panel". Nobody said, so the class is
    unanswered and the gate is capped at CONFIRM.
    """
    panelised = evidence.get("panelised")
    record = evidence.get("depanel_record")
    if panelised is False:
        return (Finding("depanel_trial", NO_COVERAGE, 0, [],
                        "the build is declared unpanelised, so there is no "
                        "depanel trial to run; recorded as an explicit "
                        "exclusion, not as a pass"), [])
    if panelised is None:
        return (Finding("depanel_trial", NO_COVERAGE, 0, [],
                        "nobody stated whether this build was panelised; the "
                        "depanel class is unanswered"), ["panelised_unknown"])
    if not isinstance(record, dict):
        return (Finding("depanel_trial", NO_COVERAGE, 0, [],
                        "the build is panelised and no depanel-trial record "
                        "was supplied"), [])
    usable, reason = evidence_usable(record.get("evidence"))
    if not usable:
        return (Finding("depanel_trial", NO_COVERAGE, 0, [],
                        f"the depanel record cannot be cited: {reason}"), [])
    violations: list[dict] = []
    interpretive: list[str] = []
    if not record.get("performed"):
        violations.append({"problem": "DEPANEL_TRIAL_NOT_PERFORMED"})
    if record.get("result") == "FAIL":
        violations.append({
            "problem": "DEPANEL_DAMAGE",
            "damaged_refdes": record.get("damaged_refdes", []),
            "feedback_target": "dfm",
            "message": "depanel stress damaged parts; the placement/panel "
                       "constraint belongs upstream, not here"})
    elif record.get("result") == "REQUIRES_INTERPRETATION":
        interpretive.append("depanel_trial")
    return (result("depanel_trial", 1, violations,
                   "de-panel trial result on the first article"), interpretive)


def check_fab_acceptance(evidence: dict) -> Finding:
    """The fab's own formal acceptance of the released package."""
    block = evidence.get("fab_dfm_acceptance")
    if not isinstance(block, dict):
        return Finding("fab_dfm_acceptance", NO_COVERAGE, 0, [],
                       "no fab DFM acceptance record was supplied; the fab "
                       "taking the order is not the fab accepting the package")
    usable, reason = evidence_usable(block.get("evidence"))
    if not usable:
        return Finding("fab_dfm_acceptance", NO_COVERAGE, 0, [],
                       f"the fab acceptance record cannot be cited: {reason}")
    violations = [] if block.get("accepted") else [{
        "problem": "FAB_REJECTED_PACKAGE",
        "notes": block.get("notes"),
        "feedback_target": "dfm",
        "message": "the fab did not accept the released package"}]
    return result("fab_dfm_acceptance", 1, violations,
                  "the fab's formal acceptance of the released package")


def check_deviations(evidence: dict) -> tuple[Finding, list]:
    """Unresolved deviations, and dispositions with nobody's name on them."""
    deviations = [d for d in evidence.get("deviations", []) or []
                  if isinstance(d, dict)]
    violations: list[dict] = []
    for deviation in deviations:
        problems = []
        if not deviation.get("resolved"):
            problems.append("UNRESOLVED_DEVIATION")
        if deviation.get("disposition") in (None, "", "open"):
            problems.append("NO_DISPOSITION")
        if not str(deviation.get("approver") or "").strip():
            problems.append("NO_APPROVER")
        for problem in problems:
            violations.append({
                "deviation_id": deviation.get("id"),
                "refdes": deviation.get("refdes"),
                "problem": problem,
                "disposition": deviation.get("disposition"),
                "approver": deviation.get("approver"),
                "feedback_target": deviation.get("feedback_target")})
    if not deviations:
        # No deviations is a real, common and clean state - unlike "no
        # inspection records", which is an absence of evidence. There is
        # nothing to measure here, so this check is deliberately excluded
        # from the rollup by the caller rather than dragging it to
        # NO_COVERAGE. Finding still refuses to call it PASS.
        return (Finding("deviation_disposition", NO_COVERAGE, 0, [],
                        "no manufacturing deviation was raised on this unit"),
                deviations)
    return (result("deviation_disposition", len(deviations), violations,
                   "every deviation carries a disposition and a named "
                   "approver, and is resolved"), deviations)


def check_signoff_standards(evidence: dict) -> tuple[Finding, list]:
    """Which acceptances rest on an observation with no stated criterion.

    A photograph is not an acceptance criterion. When the only thing standing
    behind a result is a PHOTO or MANUAL record with no ``signoff_standard``,
    the honest verdict for that result is SCREEN_ONLY: somebody looked, nobody
    said what "good" was.
    """
    records = [r for r in _all_evidence_records(evidence)]
    screen_only = sorted({r.get("id") for r in records
                          if evidence_is_screen_only(r)})
    violations: list[dict] = []       # never a FAIL - SCREEN_ONLY is not RED
    detail = ("interpretive (PHOTO/MANUAL) evidence must name the standard "
              "it was judged against")
    if not records:
        return (Finding("evidence_signoff_standard", NO_COVERAGE, 0, [],
                        "no evidence record at all to check for a signoff "
                        "standard"), [])
    finding = result("evidence_signoff_standard", len(records), violations,
                     detail + (f"; {len(screen_only)} record(s) carry none"
                               if screen_only else ""))
    return finding, screen_only


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def gate_verdict(findings, *, stale: bool, red: bool,
                 no_coverage_reasons, confirm_reasons, screen_only_reasons,
                 excluded_checks=frozenset()) -> tuple[str, list[str]]:
    """The single verdict, from the closed vocabulary, plus its reasons.

    GREEN is the only branch that opens serial production, and it is reached
    only by falling through every other one. There is no code path that
    manufactures a GREEN from an absent check: a check that measured nothing
    contributes a NO_COVERAGE reason, and a NO_COVERAGE reason is returned
    before GREEN is ever considered.
    """
    reasons: list[str] = []
    measured_failure = red or any(f["status"] == FAIL for f in findings)
    unmeasured = list(no_coverage_reasons) + [
        f"{f['check']}: {f['detail']}" for f in findings
        if f["status"] == NO_COVERAGE and f["check"] not in excluded_checks]

    buckets = {
        STALE: ["evidence or build is bound to a different board/package "
                "revision"] if stale else [],
        NO_COVERAGE: unmeasured,
        RED: [f"{f['check']}: {len(f['violations'])} violation(s)"
              for f in findings if f["status"] == FAIL] or
             (["as-built identity does not match the released package"]
              if red else []),
        CONFIRM: list(confirm_reasons),
        SCREEN_ONLY: list(screen_only_reasons),
    }
    for verdict in VERDICT_PRECEDENCE:
        if buckets[verdict]:
            reasons = sorted(set(buckets[verdict]))
            # Everything that also applies is still reported, so a reader
            # never has to re-run the gate to discover the second blocker.
            for other in VERDICT_PRECEDENCE:
                if other != verdict and buckets[other]:
                    reasons += [f"[{other}] {r}" for r in sorted(set(buckets[other]))]
            return verdict, reasons
    if measured_failure:                       # pragma: no cover - guard
        return RED, ["a measured failure was not classified; failing closed"]
    return GREEN, []


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def evaluate(evidence: dict, *, rotation_tolerance_deg: float = 1.0,
             verify_package=None, out_of_scope_reason: str | None = None) -> dict:
    """Run every check and build the first-article report. No I/O side effects."""
    universe, records, excluded, unusable = partition_universe(evidence)
    excluded_ids = {e["refdes"] for e in excluded}

    binding_finding, binding_state = check_released_package(evidence, verify_package)
    revision_finding = check_evidence_revision(evidence)

    # Called twice on purpose. The first call answers "which eligible refdes
    # is nobody accounting for?", which the presence check needs before it can
    # raise them; the second call is the block that ships, once the as-built
    # checks have said which SCANNED refdes actually carry a nonconformance.
    # Counting violations before running the checks would publish a
    # permanently-zero violations field beside a RED verdict.
    _, unaccounted = coverage_block(list(universe), list(records), excluded, [])

    presence = check_presence(universe, records, unaccounted, unusable)
    value = check_attribute(universe, records, "value", "as_built_value")
    package = check_attribute(universe, records, "package", "as_built_package")
    orientation = check_orientation(universe, records, rotation_tolerance_deg)

    as_built_findings = (presence, value, package, orientation)
    violating_refdes = sorted({
        v["refdes"] for finding in as_built_findings
        for v in finding["violations"]
        if isinstance(v, dict) and v.get("refdes")})
    top_block, _ = coverage_block(list(universe), list(records), excluded,
                                  violating_refdes)

    xray, xray_block, xray_open, xray_unrecorded = check_hidden_joints(
        universe, excluded_ids, evidence.get("xray_records"))
    msl, msl_block, msl_open, msl_unrecorded = check_msl_handling(
        universe, excluded_ids, evidence.get("msl_records"))
    depanel, depanel_open = check_depanel(evidence)
    fab = check_fab_acceptance(evidence)
    deviation_finding, deviations = check_deviations(evidence)
    signoff, screen_only_ids = check_signoff_standards(evidence)

    findings = [binding_finding, revision_finding, presence, value, package,
                orientation, xray, msl, depanel, fab, deviation_finding,
                signoff]

    # AN EMPTY POPULATION IS NOT A COVERAGE GAP - AND THE DIFFERENCE IS THE
    # WHOLE POINT. "Zero X-ray records over four BGAs" is a gap: a required
    # universe exists and nobody looked. "Zero X-ray records over zero BGAs"
    # is a fact about the released package: there is nothing to look at.
    # Reading the second as NO_COVERAGE would jam the gate permanently shut on
    # every board with no BGA, and a gate that can never open teaches people
    # to bypass it.
    #
    # The distinction is safe only because each empty population is derived
    # from the RELEASED PACKAGE (refdes_universe / panelised), never from the
    # evidence. Evidence cannot shrink its own denominator. Every one of these
    # exclusions is recorded, with its reason, in
    # coverage_ledger.excluded_checks - the same discipline
    # production/outputs.py's --exclude CHECK=REASON applies.
    excluded_checks: set[str] = set()
    scope_exclusions: list[dict] = []

    def _exclude(check: str, reason: str) -> None:
        excluded_checks.add(check)
        scope_exclusions.append({"check": check, "reason": reason})

    if not deviations:
        _exclude("deviation_disposition",
                 "no manufacturing deviation was raised on this unit; an "
                 "empty deviation log is a state, not a gap")
    if xray_block["eligible"] == 0:
        _exclude("hidden_joint_xray",
                 "the released package declares no hidden-joint (BGA/LGA/QFN "
                 "bottom-terminated) refdes, so the X-ray population is empty "
                 "by design rather than unexamined")
    if msl_block["eligible"] == 0:
        _exclude("msl_handling_record",
                 "the released package declares no moisture-sensitive refdes "
                 "above MSL 1, so the floor-life population is empty by "
                 "design rather than unexamined")
    if evidence.get("panelised") is False:
        _exclude("depanel_trial",
                 "the build is declared unpanelised in the released package, "
                 "so there is no de-panel operation to trial")

    confirm_reasons: list[str] = []
    screen_only_reasons: list[str] = []
    no_coverage_reasons: list[str] = []

    if binding_state == "UNBOUND":
        confirm_reasons.append(
            "the built-from package digest is missing; the built-vs-released "
            "identity requires a vendor record before it can be judged")
    xray_unclosed = sorted(set(xray_open)
                           | {e["refdes"] for e in xray_unrecorded})
    if xray_unclosed:
        confirm_reasons.append(
            "hidden-joint refdes without a closing X-ray verdict: "
            + ", ".join(xray_unclosed))
    msl_unclosed = sorted(set(msl_open)
                          | {e["refdes"] for e in msl_unrecorded})
    if msl_unclosed:
        confirm_reasons.append(
            "moisture-sensitive refdes without a closing handling record: "
            + ", ".join(msl_unclosed))
    if depanel_open:
        confirm_reasons.append(
            "the depanel class needs interpretation: " + ", ".join(depanel_open))
    # An attribute is only "unmeasured" when the RELEASED PACKAGE declared an
    # expectation for it. A symmetric passive has expected_rotation_deg=null
    # and legitimately carries no as-built angle; demanding one there would
    # park every board at CONFIRM forever over a non-question, and a gate that
    # is always amber is a gate nobody reads.
    unmeasured_attributes = sorted(
        refdes for refdes, row in records.items()
        if (universe[refdes].get("value") is not None
            and row.get("value") is None)
        or (universe[refdes].get("expected_rotation_deg") is not None
            and row.get("rotation_deg") is None))
    if unmeasured_attributes:
        confirm_reasons.append(
            "as-built rows with no recorded value and/or rotation where the "
            "released package declares one: " + ", ".join(unmeasured_attributes))
    if screen_only_ids:
        screen_only_reasons.append(
            "acceptance rests on interpretive evidence with no signoff "
            "standard: " + ", ".join(str(i) for i in screen_only_ids))

    if out_of_scope_reason:
        # The conservative branch of the open question below. Declaring the
        # gate out of scope records WHO decided and WHY; it does not
        # manufacture a pass. The verdict stays NO_COVERAGE.
        no_coverage_reasons.append(
            "the first-article gate was declared out of scope for this "
            f"project: {out_of_scope_reason}. A declared exclusion is "
            "recorded, never silently absent, and never GREEN.")
        scope_exclusions.append({"check": "first_article_gate",
                                 "reason": out_of_scope_reason})

    verdict, reasons = gate_verdict(
        findings,
        stale=(binding_state == "REVISION_MISMATCH"
               or revision_finding["status"] == FAIL),
        red=(binding_state == "HASH_MISMATCH"),
        no_coverage_reasons=no_coverage_reasons,
        confirm_reasons=confirm_reasons,
        screen_only_reasons=screen_only_reasons,
        excluded_checks=excluded_checks)

    identity = evidence.get("identity", {})
    released = evidence.get("released_package", {})
    blockers = [{"code": v.get("problem", "VIOLATION"),
                 "message": json.dumps(v, ensure_ascii=False, sort_keys=True)}
                for finding in findings if finding["status"] == FAIL
                for v in finding["violations"]]

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        # Loud rather than absent, exactly as common.findings.board_report
        # does for a board it cannot hash: a report nobody can tie back to the
        # evidence document it read is SCREEN_ONLY however good its contents,
        # and a MISSING field reads as an oversight while a null reads as the
        # fact it is. The CLI overwrites this with the real path + digest.
        "evidence": {"path": None, "sha256": None},
        "identity": {
            "board_revision": identity.get("board_revision"),
            "board_hash": identity.get("board_hash"),
            "lot": identity.get("lot"),
            "work_order": identity.get("work_order"),
            "unit_serial": identity.get("unit_serial"),
            "assembler": identity.get("assembler"),
            "released_package_revision": released.get("board_revision"),
            "released_package_sha256": released.get("sha256"),
            "built_from_package_sha256": identity.get("released_package_sha256"),
            "package_binding": binding_state,
        },
        "gate": {
            "verdict": verdict,
            "verdict_reasons": reasons,
            "serial_release": verdict == GREEN,
            "release_meaning": "accepted first article",
            "assurance_loop": ["L3", "L2"],
            "evidence_origin": ["PHYSICAL_MEASUREMENT", "DESIGN_ARTIFACT",
                                "TOOL_REPORT"],
            "closed_by": "tools/scripts/gate_receipt.py",
        },
        "coverage": top_block,
        "coverage_by_class": {
            "hidden_joint_xray": xray_block,
            "msl_handling_record": msl_block,
        },
        "coverage_ledger": {
            "explicitly_excluded": excluded,
            "unaccounted_folded_into_excluded": unaccounted,
            "class_unrecorded": {"hidden_joint_xray": xray_unrecorded,
                                 "msl_handling_record": msl_unrecorded},
            "excluded_checks": scope_exclusions,
        },
        "metrics": {f["check"]: (None if f["status"] == NO_COVERAGE
                                 else len(f["violations"]))
                    for f in findings},
        "by_check": by_check_map(findings),
        "checks": findings,
        "summary": summarise(findings),
        "blockers": blockers,
        "feedback_edges": sorted({
            v["feedback_target"]
            for finding in findings for v in finding["violations"]
            if isinstance(v, dict) and v.get("feedback_target")
        } | {d["feedback_target"] for d in deviations
             if d.get("feedback_target") and not d.get("resolved")}),
        "parent_receipts": evidence.get("parent_receipts", []),
        "params": {
            "rotation_tolerance_deg": rotation_tolerance_deg,
            "verified_package_path": (str(verify_package)
                                      if verify_package else None),
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "declared_out_of_scope": out_of_scope_reason,
        },
    }
    return report


def deviation_log(evidence: dict, report: dict) -> dict:
    """``manufacturing_deviation_log.json`` - the durable RED-side artifact.

    Written for every run, including GREEN ones (where it is empty and says
    so). A log that only exists when something went wrong cannot be used to
    prove that nothing did.
    """
    return {
        "schema_version": "manufacturing-deviation-log/v1",
        "tool": TOOL_NAME,
        "identity": report["identity"],
        "gate_verdict": report["gate"]["verdict"],
        "deviations": [d for d in evidence.get("deviations", []) or []
                       if isinstance(d, dict)],
        "open_deviations": [d.get("id") for d in evidence.get("deviations", []) or []
                            if isinstance(d, dict) and not d.get("resolved")],
        "feedback_edges": report["feedback_edges"],
        "blockers": report["blockers"],
    }


def acceptance_receipt(report: dict, evidence: dict, *, argv, exit_code,
                       start_utc: str, end_utc: str,
                       evidence_path=None, config_path=None) -> dict:
    """``assembly_acceptance_receipt.json`` in ``gate-receipt/v1`` shape.

    HONESTY NOTE, and it matters: this is a receipt FRAGMENT. It carries the
    fields ``gate_receipt.py`` requires and it is written with the same names,
    but this tool does NOT validate it - ``python3 tools/scripts/gate_receipt.py
    assembly_acceptance_receipt.json`` is what closes the gate, and it is the
    only thing that may report the receipt GREEN. A tool that graded its own
    receipt would be marking its own homework.

    ``metric_semantics`` is filled for every parsed metric leaf because the
    validator requires all six fields to be non-empty. Four of those six are
    geometry fields (distance_semantics, region, sampling_step,
    geometry_model) and a refdes COUNT has no geometry. They say so in words
    rather than being given a plausible-looking value: an invented
    "centerline" on a counting metric is worse than an honest
    "not_applicable".
    """
    counted = report["coverage"]
    inputs = []
    if evidence_path is not None:
        inputs.append({"role": "fai_evidence", "path": str(evidence_path),
                       "sha256": sha256_file(evidence_path)})
    for artifact in evidence.get("released_package", {}).get("artifacts", []) or []:
        if isinstance(artifact, dict):
            inputs.append({"role": artifact.get("role", "released_artifact"),
                           "path": artifact.get("path"),
                           "sha256": artifact.get("sha256")})
    not_applicable = ("not_applicable: this is a count over the released "
                      "refdes universe, not a geometric measurement")
    semantics = {
        f"first_article.{name}": {
            "definition": definition,
            "units": "count",
            "distance_semantics": not_applicable,
            "region": "the released CPL/BOM refdes universe of this board "
                      "revision",
            "sampling_step": "one record per refdes; no resampling",
            "geometry_model": not_applicable,
        }
        for name, definition in (
            ("eligible", "refdes in the released package's first-article scope"),
            ("scanned", "eligible refdes with a citable inspection record"),
            ("excluded", "eligible refdes deliberately or unaccountably out of "
                         "scope, each with a recorded reason"),
            ("violations", "scanned refdes carrying at least one as-built "
                           "nonconformance"),
        )
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "gate_id": f"first-article/{report['identity'].get('board_revision')}/"
                   f"{report['identity'].get('unit_serial')}",
        "gate_kind": "first_article_assembly_acceptance",
        "verdict": report["gate"]["verdict"],
        "start_utc": start_utc,
        "end_utc": end_utc,
        "tool": {"name": TOOL_NAME, "version": SCHEMA_VERSION,
                 "path": str(Path(__file__).resolve()),
                 "sha256": sha256_file(Path(__file__).resolve())},
        "inputs": inputs,
        "config": ({"path": str(config_path), "sha256": sha256_file(config_path)}
                   if config_path and Path(config_path).is_file()
                   else {"path": None, "sha256": None}),
        "argv": list(argv),
        "exit_code": exit_code,
        "parsed_metrics": {"first_article": {
            "eligible": counted["eligible"],
            "scanned": counted["scanned"],
            "excluded": counted["excluded"],
            "violations": counted["violations"],
        }},
        "metric_semantics": semantics,
        "coverage": dict(counted, **(
            {"reason": "; ".join(report["gate"]["verdict_reasons"])
                       or "nothing was inspected"}
            if counted["scanned"] == 0 else {})),
        "blockers": report["blockers"],
        "child_receipts": [
            {"role": r.get("role"), "path": r.get("path"),
             "sha256": r.get("sha256"), "verdict": r.get("verdict"),
             "mandatory": True}
            for r in report.get("parent_receipts", []) if isinstance(r, dict)],
        "retention": {
            "policy": "indefinite",
            "retain": (["receipt", "hashes", "metrics",
                        "reproducibility_command"]
                       if report["gate"]["verdict"] == GREEN
                       else ["receipt", "child_receipts", "logs", "scratch"]),
        },
        "validated_by": "tools/scripts/gate_receipt.py",
        "$comment": "FRAGMENT: written by first_article.py, NOT validated by "
                    "it. gate_receipt.py is what closes this gate.",
    }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def _evidence_ref(eid="EV-1", etype="AOI", standard="IPC-A-610 Class 2"):
    return {"id": eid, "type": etype, "locator": f"evidence/{eid}.json",
            "sha256": "a" * 64, "recorded_utc": "2026-08-10T09:00:00Z",
            "signoff_standard": standard, "operator": "QA-7"}


def _clean_evidence() -> dict:
    """A complete, conforming first article. Every self-test fault starts here."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "identity": {"board_revision": "rev-C", "lot": "L-31",
                     "work_order": "WO-4412", "unit_serial": "SN-0001",
                     "released_package_sha256": "b" * 64,
                     "assembler": "ACME EMS"},
        "released_package": {"board_revision": "rev-C", "sha256": "b" * 64,
                             "artifacts": []},
        "panelised": False,
        "refdes_universe": [
            {"refdes": "U1", "value": "DS90UB953", "package": "QFN-32",
             "expected_rotation_deg": 270, "side": "top", "polar": True,
             "hidden_joint": False, "msl_level": "1"},
            {"refdes": "D1", "value": "1N4148", "package": "SOD-323",
             "expected_rotation_deg": 90, "side": "top", "polar": True,
             "hidden_joint": False, "msl_level": "1"},
        ],
        "as_built": [
            {"refdes": "U1", "present": True, "value": "DS90UB953",
             "package": "QFN-32", "rotation_deg": 270,
             "evidence": _evidence_ref("AOI-1")},
            {"refdes": "D1", "present": True, "value": "1N4148",
             "package": "SOD-323", "rotation_deg": 90,
             "evidence": _evidence_ref("AOI-2")},
        ],
        "fab_dfm_acceptance": {"accepted": True,
                               "evidence": _evidence_ref("FAB-1",
                                                         "VENDOR_REPORT")},
        "deviations": [],
        "parent_receipts": [],
    }


def self_test() -> list[str]:
    """Fault injection only. Touches no external system and no image."""
    problems: list[str] = []

    clean = evaluate(_clean_evidence())
    if clean["gate"]["verdict"] != GREEN:
        problems.append(f"a conforming first article was not GREEN: "
                        f"{clean['gate']['verdict_reasons']}")
    if not clean["gate"]["serial_release"]:
        problems.append("a GREEN first article did not release serial build")

    # FAULT INJECTION 1: one rotation record flipped. This is the defect the
    # whole gate exists for and it must turn GREEN into RED.
    flipped = _clean_evidence()
    flipped["as_built"][0]["rotation_deg"] = 90
    report = evaluate(flipped)
    if report["gate"]["verdict"] != RED:
        problems.append(f"a reversed part did not turn the gate RED: "
                        f"{report['gate']['verdict']}")
    if report["gate"]["serial_release"]:
        problems.append("a RED gate still released serial build")

    # FAULT INJECTION 2: the assembler built from different package bytes.
    substituted = _clean_evidence()
    substituted["identity"]["released_package_sha256"] = "c" * 64
    if evaluate(substituted)["gate"]["verdict"] != RED:
        problems.append("a released-package hash mismatch did not block")

    # FAULT INJECTION 3: evidence from another revision is STALE, not RED.
    stale = _clean_evidence()
    stale["identity"]["board_revision"] = "rev-B"
    if evaluate(stale)["gate"]["verdict"] != STALE:
        problems.append("evidence from another board revision was not STALE")

    # FAULT INJECTION 4: a BGA with no X-ray can never be GREEN.
    bga = _clean_evidence()
    bga["refdes_universe"][0]["hidden_joint"] = True
    verdict = evaluate(bga)["gate"]["verdict"]
    if verdict == GREEN:
        problems.append("a hidden-joint package with no X-ray reported GREEN")
    if verdict not in (NO_COVERAGE, CONFIRM):
        problems.append(f"an unclosed hidden-joint universe reported {verdict}")

    # FAULT INJECTION 5: nobody inspected anything.
    empty = _clean_evidence()
    empty["as_built"] = []
    blank = evaluate(empty)
    if blank["gate"]["verdict"] != NO_COVERAGE:
        problems.append(f"a zero-record inspection reported "
                        f"{blank['gate']['verdict']}")
    if blank["coverage"]["scanned"] != 0:
        problems.append("an empty inspection claimed a non-zero scanned count")

    # FAULT INJECTION 6: photo-only evidence with no criterion.
    photo = _clean_evidence()
    for row in photo["as_built"]:
        row["evidence"] = _evidence_ref(row["refdes"], "PHOTO", None)
    if evaluate(photo)["gate"]["verdict"] != SCREEN_ONLY:
        problems.append("photo-only evidence with no signoff standard was not "
                        "SCREEN_ONLY")

    # The coverage arithmetic must hold in EVERY report, including the broken
    # ones - a report that cannot be validated cannot be argued with.
    for name, doc in (("clean", clean), ("flipped", report), ("blank", blank)):
        for label, block in [("coverage", doc["coverage"])] + \
                list(doc["coverage_by_class"].items()):
            if coverage_rule_violations(block):
                problems.append(f"{name}.{label} broke coverage arithmetic: "
                                f"{block}")

    # An unaccounted refdes must be folded AND raised, never just folded.
    orphan = _clean_evidence()
    orphan["as_built"] = orphan["as_built"][:1]
    doc = evaluate(orphan)
    if doc["coverage"]["scanned"] + doc["coverage"]["excluded"] != \
            doc["coverage"]["eligible"]:
        problems.append("an unaccounted refdes broke the coverage sum")
    if not doc["coverage_ledger"]["unaccounted_folded_into_excluded"]:
        problems.append("an unaccounted refdes was not recorded in the ledger")
    if doc["gate"]["verdict"] == GREEN:
        problems.append("an unaccounted refdes still reported GREEN")

    # Declaring the gate out of scope records the decision; it never passes.
    scoped = evaluate(_clean_evidence(),
                      out_of_scope_reason="prototype-only build, NPI board 1")
    if scoped["gate"]["verdict"] != NO_COVERAGE:
        problems.append("an out-of-scope declaration produced "
                        f"{scoped['gate']['verdict']} instead of NO_COVERAGE")

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("evidence", nargs="?",
                        help="fai_evidence.json (schema: "
                             "skills/ilk-urun-montaj-kabulu/references/"
                             "fai_evidence.schema.json)")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--receipt-out",
                        help="write assembly_acceptance_receipt.json (a "
                             "gate-receipt/v1 FRAGMENT; gate_receipt.py is "
                             "what validates and closes it)")
    parser.add_argument("--deviation-log-out",
                        help="write manufacturing_deviation_log.json")
    parser.add_argument("--verify-package", metavar="PATH",
                        help="re-hash the released package on disk and compare "
                             "it with the digest the release record claims")
    parser.add_argument("--rotation-tolerance-deg", type=float, default=1.0)
    parser.add_argument("--declare-out-of-scope", metavar="REASON",
                        help="record that this project does not run a "
                             "first-article gate, with the reason. This is an "
                             "EXPLICIT RECORDED EXCLUSION, not a pass: the "
                             "verdict becomes NO_COVERAGE and serial release "
                             "stays shut. There is no flag that makes an "
                             "unrun gate GREEN.")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: first_article")
        return 0

    if not args.evidence:
        parser.error("the FAI evidence JSON is required")

    start_utc = _utc_now()
    evidence = load_evidence(args.evidence)
    report = evaluate(
        evidence,
        rotation_tolerance_deg=args.rotation_tolerance_deg,
        verify_package=args.verify_package,
        out_of_scope_reason=args.declare_out_of_scope)
    report["evidence"] = {"path": str(args.evidence),
                          "sha256": sha256_file(args.evidence)}

    verdict = report["gate"]["verdict"]
    exit_code = status_exit_code(verdict)

    if args.deviation_log_out:
        write_report(deviation_log(evidence, report), args.deviation_log_out)
    if args.receipt_out:
        schema = (Path(__file__).resolve().parents[3] / "skills"
                  / "ilk-urun-montaj-kabulu" / "references"
                  / "fai_evidence.schema.json")
        write_report(
            acceptance_receipt(
                report, evidence,
                argv=[sys.argv[0]] + list(argv or sys.argv[1:]),
                exit_code=exit_code, start_utc=start_utc, end_utc=_utc_now(),
                evidence_path=args.evidence,
                config_path=schema if schema.is_file() else None),
            args.receipt_out)
    write_report(report, args.json)
    emit(report)

    if verdict != GREEN:
        diag(f"SERIAL BUILD BLOCKED ({verdict}): "
             + "; ".join(report["gate"]["verdict_reasons"]))
        if report["feedback_edges"]:
            diag("L2 feedback belongs to: " + ", ".join(report["feedback_edges"]))
    return exit_code


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
