#!/usr/bin/env python3
"""Ingest physical measurement records, bind them to a unit, and close (or
refuse to close) the D-level rows of the verification matrix.

WHY THIS TOOL EXISTS
--------------------
Ten skills list ``L3`` in their ``assurance_loop`` and all eighteen declare
``evidence_origin: TOOL_REPORT``. Read together those two facts say something
uncomfortable: nothing in this bundle has ever produced a
``PHYSICAL_MEASUREMENT``. ``dogrulama-matrisi`` publishes the D rows - real
rail sequencing, real temperature under real load, coupon/TDR impedance,
channel behaviour, EMC/ESD - and publishes no producer for them, so each
domain skill pushes the row into its own "cannot prove" table and the row
stays there. A gap that every table acknowledges and no tool owns is the
quietest kind of open gap: every artifact gate can be GREEN while the board
has never been switched on.

This is the producer. Its question is narrow and it is the only question it
answers:

    is there VALID PHYSICAL EVIDENCE for this D row on THIS accepted unit,
    and which side of the stated limit is the result on?

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* **It talks to no instrument and takes no measurement.** There is no VISA
  session, no SCPI string, no capture loop, and there will not be one. A
  measurement this tool produced itself would be a second answer to a question
  the bench had already answered, and the report could not tell the two apart.
  What arrives is a file the instrument wrote plus a lab record describing how
  it was taken; what leaves is a decision about whether that pair is evidence.

* **It invents no threshold.** Not one. Every limit arrives as an INPUT
  carrying its own authority binding, and the authority's owning skill is
  named in the report. ``dogrulama-matrisi`` and the domain skills
  (``guc-isil``, ``empedans-stackup``, ``emc-esd``, ``highspeed-routing``) own
  the limits; this tool owns evidence sufficiency. A row whose limit was not
  supplied is ``NO_COVERAGE`` - never GREEN, and never a default. A limit
  whose authority names *this* skill is refused outright: a gate that writes
  its own threshold and then measures against it is a tautology, and this
  bundle has found that false-PASS class more than once
  (``skills/_shared/rule-conflicts.md`` §9).

* **It adds no test point and patches no board.** ``dft-testpoints`` owns the
  probe/fixture accessibility plan. On RED this tool opens an L2 feedback edge
  to the rule's declared owner and writes ``l3_feedback_actions.json``; it does
  not touch the design.

* **It does not upgrade a screen into a certification.** A record declaring
  ``compliance_class: pre_compliance`` is reported ``SCREEN_ONLY`` and is never
  compared against a formal limit, because a near-field dBuV number cannot be
  compared with a laboratory limit at all - only with its own baseline in its
  own setup (``skills/emc-esd/SKILL.md`` §8, §9). SCREEN_ONLY does not close a
  mandatory row.

WHAT MAKES A RECORD ADMISSIBLE
------------------------------
In order, because the order is the argument:

  1. the record declares ``evidence_origin: PHYSICAL_MEASUREMENT``;
  2. it is bound to the ACCEPTED unit - board_revision, board_hash, serial and
     lot all match the first-article acceptance (``ilk-urun-montaj-kabulu``).
     A mismatch is ``STALE``: a measurement of a different board is not weaker
     evidence about this one, it is no evidence about this one;
  3. the instrument carries a calibration id AND date. Uncalibrated is not
     measured, so it is ``NO_COVERAGE`` rather than a slightly worse number;
  4. method, setup, environment and load_state are recorded. A setup nobody
     wrote down cannot be repeated, and a measurement that cannot be repeated
     is not evidence (``emc-esd`` §8 states this for pre-compliance; it is
     true of every row here);
  5. for TDR/VNA/S-parameter evidence, additionally reference_plane,
     measurement_zone, port_map, reference_impedance, fixture and
     deembed_method. This is not a new rule - it is
     ``skills/highspeed-routing/SKILL.md`` §6 verbatim, and its stated
     consequence for an incomplete set is ``NO_COVERAGE`` or
     ``INVALID_SPARAM``. This tool is that discipline's physical producer, so
     it enforces the rule rather than restating it;
  6. the raw capture hashes to the declared ``raw_data_sha256``;
  7. the units match the limit's units. They usually do; when they do not, the
     comparison is arithmetic on two different quantities and the answer would
     look exactly like a real one - ``INVALID_METRIC``.

Only then is the value compared, and it is compared as an INTERVAL:
``[value - (uncertainty + guard_band), value + (uncertainty + guard_band)]``.
Wholly inside the limit is GREEN, wholly outside is RED, and straddling the
boundary is ``INCONCLUSIVE`` - never a PASS on the strength of the point
estimate (``highspeed-routing`` §6: "marj uncertainty + guard_band icinde
kaliyorsa PASS verme").

INPUTS
------
--rules RULES.json          the D-row universe with limits AND authorities,
                            from dogrulama-matrisi
--records RECORDS.json      lab records; schema in
                            skills/l3-bringup-karakterizasyon/references/
                            measurement_record.schema.json
--accepted-unit FAI.json    first_article_report.json from
                            ilk-urun-montaj-kabulu. Its acceptance is a
                            PRECONDITION: an unaccepted unit is RED, because
                            hunting electrical faults on a unit whose assembly
                            was never accepted attributes build defects to the
                            design (rule-conflicts §9).

OUTPUTS
-------
physical_measurement_manifest.json   (--json / --manifest)  the calibrated
                                     manifest: every record, why it was or was
                                     not admitted, and the coverage arithmetic
l3_gate_receipt.json                 (--receipt)
l3_feedback_actions.json             (--feedback)  the L2 edges, each naming
                                     the upstream owner of the rule it broke

Exit codes: 0 GREEN, 1 RED / NO_COVERAGE / INCONCLUSIVE / SCREEN_ONLY /
CONFIRM / STALE / INVALID_SPARAM, 2 could not answer.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    acceptance_verdict, coverage_rule_violations,
)
from common.hashing import sha256_file, valid_digest  # noqa: E402
from common.report_io import (  # noqa: E402
    EXIT_ERROR, diag, emit, error_report, run_cli, status_exit_code,
    write_report,
)

SCHEMA_VERSION = "physical-measurement-manifest/v1"
RECEIPT_SCHEMA_VERSION = "l3-gate-receipt/v1"
FEEDBACK_SCHEMA_VERSION = "l3-feedback-actions/v1"
TOOL_VERSION = "physical-measurement/v1"
TOOL_NAME = "physical-measurement"
MODULE_NAME = "physical_measurement.py"
LOCATOR = "tools/scripts/lab/physical_measurement.py"
GATE_ID = "l3-bringup-karakterizasyon"
SELF = Path(__file__).resolve()

# ---- the closed verdict vocabulary (skills/_shared/coverage-gate.md) ------- #
GREEN = "GREEN"
RED = "RED"
NO_COVERAGE = "NO_COVERAGE"
INCONCLUSIVE = "INCONCLUSIVE"
INVALID_METRIC = "INVALID_METRIC"
INVALID_SPARAM = "INVALID_SPARAM"
INVALID_COVERAGE = "INVALID_COVERAGE"
CONFIRM = "CONFIRM"
SCREEN_ONLY = "SCREEN_ONLY"
STALE = "STALE"

# Worst first. Adding evidence can only move a row DOWN this list, never up:
# one RED row is a RED campaign no matter how many GREEN rows sit beside it,
# and one unmeasured mandatory row cannot be outvoted by measured ones.
SEVERITY = (INVALID_COVERAGE, RED, STALE, INVALID_SPARAM, INVALID_METRIC,
            NO_COVERAGE, INCONCLUSIVE, CONFIRM, SCREEN_ONLY, GREEN)

# A row counts as SCANNED only when a real comparison happened against a real
# limit. Everything else is an exclusion carrying its reason - which is why
# `violations <= scanned` can never be violated: a RED row was, by definition,
# compared.
JUDGED = (GREEN, RED, INCONCLUSIVE)

PHYSICAL = "PHYSICAL_MEASUREMENT"

# pre_compliance < bench < accredited_lab. The ordering exists so a rule can
# demand a class and a weaker record can be reported CONFIRM instead of being
# quietly accepted.
COMPLIANCE_CLASSES = ("pre_compliance", "bench", "accredited_lab")

SPARAM_CLASSES = frozenset({"tdr", "vna", "sparam", "touchstone"})

RECORD_REQUIRED = (
    "measurement_id", "rule_id", "evidence_origin", "measurement_class",
    "board_revision", "board_hash", "serial", "lot",
    "instrument_make", "instrument_model", "instrument_serial",
    "calibration_id", "calibration_date",
    "method", "setup", "environment", "load_state",
    "value", "units", "uncertainty", "guard_band",
    "raw_data_sha256", "compliance_class", "assessor", "timestamp",
)

# highspeed-routing/SKILL.md:349-351, enforced rather than restated.
SPARAM_REQUIRED = ("reference_plane", "measurement_zone", "port_map",
                   "reference_impedance", "fixture", "deembed_method")

AUTHORITY_REQUIRED = ("owner_skill", "document_id", "locator")

ACCEPTED_VERDICTS = frozenset({"GREEN", "ACCEPTED", "PASS"})

CAMPAIGN_STAGES = ("power_safe", "functional", "characterisation")

LIMITS = [
    "this tool takes no measurement: it decides whether a measurement someone "
    "else took is admissible evidence for a named rule on a named unit",
    "it invents no threshold; a row whose limit did not arrive with an "
    "authority binding is NO_COVERAGE, never GREEN",
    "a limit whose authority names this skill is refused - a gate that writes "
    "its own threshold measures itself",
    "uncalibrated is not measured: a record without calibration id and date is "
    "NO_COVERAGE, not a slightly worse number",
    "a setup that was not written down cannot be repeated, so it is not "
    "evidence (NO_COVERAGE)",
    "pre-compliance is SCREEN_ONLY and is never compared against a formal "
    "limit nor upgraded to GREEN; accredited compliance needs the lab artifact",
    "the value is compared as an interval widened by uncertainty + guard_band; "
    "a margin inside that interval is INCONCLUSIVE, not a PASS",
    "GREEN here is evidence-sufficiency closure for the rows whose limits were "
    "supplied - it is not an accreditation and signoff stays false",
]


class MeasurementIngestError(RuntimeError):
    """The ingest could not be performed at all."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return None
    return numeric


def worst(statuses: Sequence[str]) -> str:
    """Monotone rollup over the closed vocabulary. Empty is NO_COVERAGE."""
    present = set(statuses)
    for status in SEVERITY:
        if status in present:
            return status
    return NO_COVERAGE


def load_json(path: Path, role: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise MeasurementIngestError(f"cannot read {role} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MeasurementIngestError(f"{role} {path} is not JSON: {exc}") from exc


def _listify(payload: Any, key: str, role: str) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get(key)
        if items is None:
            raise MeasurementIngestError(
                f"{role} carries no '{key}' array. This tool reads records "
                f"someone else wrote; it does not manufacture the list")
    else:
        raise MeasurementIngestError(f"{role}: expected an object or an array")
    if not isinstance(items, list):
        raise MeasurementIngestError(f"{role}: '{key}' must be an array")
    bad = [i for i, item in enumerate(items) if not isinstance(item, dict)]
    if bad:
        raise MeasurementIngestError(
            f"{role}: entries {bad} are not objects")
    return list(items)


# --------------------------------------------------------------------------- #
# The accepted unit - the precondition, not a convenience
# --------------------------------------------------------------------------- #
def accepted_unit(payload: Any, path: Path) -> dict:
    """The unit these measurements are allowed to be about.

    Deliberately permissive about WHERE the fields sit. ``first-article-
    report/v1`` (``production/first_article.py``) puts them in ``identity``
    and its verdict in ``gate``; a hand-written stub is likelier to use
    ``unit`` and a top-level ``verdict``. Both are read, plus the obvious
    aliases (``unit_serial``, ``board_sha256``), because refusing a report
    over field placement would be a refusal about spelling.

    Deliberately strict about the fields THEMSELVES: a unit with no digest
    cannot be bound to, and an unaccepted unit is a precondition failure
    rather than a detail. ``gate.serial_release`` is honoured as a veto even
    when the verdict string is unfamiliar - the FAI gate owns that decision
    and this one does not re-litigate it.
    """
    if not isinstance(payload, dict):
        raise MeasurementIngestError(
            f"{path}: expected a first-article report object")

    def block(name: str) -> dict:
        value = payload.get(name)
        return value if isinstance(value, dict) else {}

    identity, unit, gate = block("identity"), block("unit"), block("gate")

    def pick(*names: str) -> str:
        for name in names:
            for source in (identity, unit, gate, payload):
                value = _text(source.get(name))
                if value:
                    return value
        return ""

    digest = pick("board_hash", "board_sha256")
    verdict = (pick("verdict", "status") or "").upper()
    release = gate.get("serial_release")
    record = {
        "board_revision": pick("board_revision", "revision"),
        "board_hash": digest,
        "serial": pick("serial", "unit_serial", "serial_number"),
        "lot": pick("lot", "lot_code"),
        "first_article": {
            "path": str(path),
            "sha256": sha256_file(path) if Path(path).is_file() else None,
            "verdict": verdict or None,
            "serial_release": release if isinstance(release, bool) else None,
            "package_binding": _text(identity.get("package_binding")) or None,
        },
    }
    missing = [key for key in ("board_revision", "board_hash", "serial", "lot")
               if not record[key]]
    if missing:
        raise MeasurementIngestError(
            f"{path}: the accepted unit is missing {', '.join(missing)}. "
            "Physical evidence that is not bound to a revision, a digest, a "
            "serial and a lot is evidence about no particular board")
    if not valid_digest(digest):
        raise MeasurementIngestError(
            f"{path}: board_hash is not a 64-character hex digest. Without a "
            "digest the campaign cannot be tied to the design it measured")
    record["accepted"] = (verdict in ACCEPTED_VERDICTS
                          and release is not False)
    return record


# --------------------------------------------------------------------------- #
# The rule side: a limit, or an honest absence of one
# --------------------------------------------------------------------------- #
def rule_limit(rule: dict) -> tuple[dict | None, str, str]:
    """``(limit, reason_code, detail)``. ``limit is None`` => the row cannot be
    judged, and nothing about the records can change that.

    This function is where the single-authority rule is mechanically enforced.
    It has no fallback branch on purpose: there is no shape of missing input
    that produces a number here.
    """
    rule_id = _text(rule.get("rule_id")) or "<unnamed>"
    if not _text(rule.get("owner")):
        return (None, "OWNER_NOT_DECLARED",
                f"{rule_id}: no owning skill is declared, so a RED result "
                "would have no upstream to feed back to")
    limit = rule.get("limit")
    if not isinstance(limit, dict):
        return (None, "LIMIT_NOT_SUPPLIED",
                f"{rule_id}: no limit was supplied. dogrulama-matrisi and the "
                "domain skills own the limits; this tool has no default and "
                "will not derive one from the measurements it is judging")
    if not _text(limit.get("units")):
        return (None, "LIMIT_UNITS_ABSENT",
                f"{rule_id}: the limit carries no units, so nothing can be "
                "compared against it without assuming what it means")
    if _number(limit.get("min")) is None and _number(limit.get("max")) is None:
        return (None, "LIMIT_BOUNDS_ABSENT",
                f"{rule_id}: the limit carries neither min nor max")
    authority = limit.get("authority")
    if not isinstance(authority, dict):
        return (None, "LIMIT_AUTHORITY_MISSING",
                f"{rule_id}: the limit arrived without an authority binding. "
                "An unsourced number is a preference, and the report cannot "
                "tell a preference from a specification")
    missing = [key for key in AUTHORITY_REQUIRED
               if not _text(authority.get(key))]
    if missing:
        return (None, "LIMIT_AUTHORITY_MISSING",
                f"{rule_id}: the limit authority is missing "
                f"{', '.join(missing)}")
    if _text(authority.get("owner_skill")) == GATE_ID:
        return (None, "SELF_AUTHORED_LIMIT",
                f"{rule_id}: the limit authority names {GATE_ID}, which is "
                "this skill. A gate that writes its own threshold and then "
                "measures against it is a tautology - the limit must come "
                "from the domain owner (rule-conflicts.md §9)")
    return (limit, "", "")


def judge(value: float, uncertainty: float, guard_band: float,
          limit: dict) -> tuple[str, str]:
    """Compare an INTERVAL against the limit, never a bare point estimate.

    ``highspeed-routing/SKILL.md`` §6: if the margin sits inside
    ``uncertainty + guard_band``, do not report PASS. The interval makes that
    mechanical: wholly inside is GREEN, wholly outside is RED, and anything
    that touches the boundary is INCONCLUSIVE - the decision boundary could
    not be closed by this measurement, which is a different statement from
    both "it passed" and "it failed".
    """
    band = abs(uncertainty) + abs(guard_band)
    low, high = value - band, value + band
    minimum = _number(limit.get("min"))
    maximum = _number(limit.get("max"))
    outside = ((minimum is not None and high < minimum)
               or (maximum is not None and low > maximum))
    inside = ((minimum is None or low >= minimum)
              and (maximum is None or high <= maximum))
    window = (f"[{'-inf' if minimum is None else minimum}, "
              f"{'+inf' if maximum is None else maximum}] "
              f"{limit.get('units')}")
    interval = f"{value} +/- {band} (uncertainty + guard_band)"
    if outside:
        return RED, f"{interval} lies outside {window}"
    if inside:
        return GREEN, f"{interval} lies inside {window}"
    return (INCONCLUSIVE,
            f"{interval} straddles an edge of {window}: the decision boundary "
            "cannot be closed by this measurement. Reduce the uncertainty or "
            "raise the evidence class; do not read the point estimate alone")


# --------------------------------------------------------------------------- #
# The record side
# --------------------------------------------------------------------------- #
def _raw_data_check(record: dict, raw_root: Path | None
                    ) -> tuple[str, str, str] | None:
    declared = _text(record.get("raw_data_sha256"))
    if not valid_digest(declared):
        return (INVALID_METRIC, "RAW_DATA_DIGEST_MALFORMED",
                "raw_data_sha256 is not a 64-character hex digest")
    relative = _text(record.get("raw_data_path"))
    if raw_root is None or not relative:
        return None
    candidate = Path(relative)
    if not candidate.is_absolute():
        candidate = Path(raw_root) / candidate
    if not candidate.is_file():
        return (NO_COVERAGE, "RAW_DATA_ABSENT",
                f"the raw capture {candidate} is not present under --raw-root, "
                "so the declared digest cannot be checked and the measurement "
                "cannot be re-examined by anyone else")
    actual = sha256_file(candidate)
    if actual != declared.lower():
        return (INVALID_METRIC, "RAW_DATA_DIGEST_MISMATCH",
                f"{candidate} hashes to {actual[:12]}... but the record "
                f"declares {declared[:12]}...: the record and the capture are "
                "not the same measurement")
    return None


def validate_record(record: dict, rule: dict, limit: dict, unit: dict,
                    raw_root: Path | None) -> tuple[str, str, str]:
    """``(verdict, reason_code, detail)`` for ONE record against ONE rule.

    The order of the checks is the argument, and each refusal is deliberately
    the honest one rather than the convenient one - see the module docstring.
    """
    identifier = _text(record.get("measurement_id")) or "<unnamed>"

    missing = [key for key in RECORD_REQUIRED if record.get(key) is None
               or (isinstance(record.get(key), str)
                   and not _text(record.get(key)))]
    if missing:
        # calibration is called out by name because it is the field whose
        # absence looks most like a formatting slip and is in fact the
        # difference between a measurement and a reading.
        if {"calibration_id", "calibration_date"} & set(missing):
            return (NO_COVERAGE, "UNCALIBRATED",
                    f"{identifier}: no calibration "
                    f"{' and '.join(sorted({'calibration_id', 'calibration_date'} & set(missing)))}"
                    ". An uncalibrated instrument did not measure; it read. "
                    "That is NO_COVERAGE, not a slightly worse number")
        if {"method", "setup", "environment", "load_state"} & set(missing):
            return (NO_COVERAGE, "SETUP_NOT_REPRODUCIBLE",
                    f"{identifier}: missing {', '.join(sorted(set(missing) & {'method', 'setup', 'environment', 'load_state'}))}"
                    ". A setup nobody wrote down cannot be repeated, and a "
                    "measurement that cannot be repeated is not evidence")
        return (INVALID_METRIC, "RECORD_FIELD_MISSING",
                f"{identifier}: missing {', '.join(sorted(missing))}")

    if _text(record.get("evidence_origin")) != PHYSICAL:
        return (NO_COVERAGE, "NOT_PHYSICAL_EVIDENCE",
                f"{identifier}: evidence_origin is "
                f"{record.get('evidence_origin')!r}, not {PHYSICAL}. This gate "
                "closes D rows with bench evidence only; a tool report is "
                "already available upstream and does not become physical here")

    if not valid_digest(_text(record.get("board_hash"))):
        return (INVALID_METRIC, "BOARD_HASH_MALFORMED",
                f"{identifier}: board_hash is not a 64-character hex digest")
    drift = [key for key in ("board_revision", "board_hash", "serial", "lot")
             if _text(record.get(key)) != unit[key]]
    if drift:
        return (STALE, "UNIT_BINDING_MISMATCH",
                f"{identifier}: {', '.join(drift)} do(es) not match the "
                f"accepted unit ({unit['board_revision']}/"
                f"{unit['serial']}/{unit['lot']}, board "
                f"{unit['board_hash'][:12]}...). A measurement of a different "
                "unit is not weaker evidence about this one - it is no "
                "evidence about this one")

    measurement_class = _text(record.get("measurement_class")).lower()
    declared_class = _text(rule.get("measurement_class")).lower()
    if declared_class and measurement_class != declared_class:
        return (INVALID_METRIC, "MEASUREMENT_CLASS_MISMATCH",
                f"{identifier}: the rule asks for a {declared_class} "
                f"measurement and this record is {measurement_class}")

    if measurement_class in SPARAM_CLASSES:
        absent = [key for key in SPARAM_REQUIRED
                  if record.get(key) in (None, "", [], {})]
        if absent:
            return (INVALID_SPARAM, "SPARAM_METADATA_INCOMPLETE",
                    f"{identifier}: TDR/VNA evidence is missing "
                    f"{', '.join(absent)}. highspeed-routing/SKILL.md §6 makes "
                    "reference plane, measurement zone, calibration, "
                    "fixture/de-embed method, port map and reference impedance "
                    "mandatory, and names this exact outcome: NO_COVERAGE or "
                    "INVALID_SPARAM. A de-embed nobody declared moves both "
                    "magnitude and delay, and no residual separates that from "
                    "the thing being measured")
        if _number(record.get("reference_impedance")) is None or \
                _number(record.get("reference_impedance")) <= 0:
            return (INVALID_SPARAM, "REFERENCE_IMPEDANCE_INVALID",
                    f"{identifier}: reference_impedance must be a positive "
                    "number of ohms")

    raw = _raw_data_check(record, raw_root)
    if raw is not None:
        return raw

    value = _number(record.get("value"))
    if value is None:
        return (INVALID_METRIC, "VALUE_NOT_NUMERIC",
                f"{identifier}: value is not a finite number")
    uncertainty = _number(record.get("uncertainty"))
    guard_band = _number(record.get("guard_band"))
    if uncertainty is None or guard_band is None:
        return (INVALID_METRIC, "UNCERTAINTY_NOT_NUMERIC",
                f"{identifier}: uncertainty and guard_band must both be finite "
                "numbers. Zero is a legitimate value and must be written down; "
                "absence is not zero")

    if _text(record.get("units")) != _text(limit.get("units")):
        return (INVALID_METRIC, "UNIT_MISMATCH",
                f"{identifier}: the record is in "
                f"{record.get('units')!r} and the limit is in "
                f"{limit.get('units')!r}. Comparing them is arithmetic on two "
                "different quantities, and the answer would look exactly like "
                "a real one")

    compliance = _text(record.get("compliance_class")).lower()
    if compliance not in COMPLIANCE_CLASSES:
        return (INVALID_METRIC, "COMPLIANCE_CLASS_UNKNOWN",
                f"{identifier}: compliance_class {compliance!r} is not one of "
                f"{list(COMPLIANCE_CLASSES)}. It has no default: the whole "
                "point of the field is that a screen and a signoff must not "
                "be able to look alike")

    if compliance == "pre_compliance":
        if not _text(record.get("baseline_id")):
            return (NO_COVERAGE, "BASELINE_ABSENT",
                    f"{identifier}: a pre-compliance record without a "
                    "baseline_id is not even a screen. emc-esd §8: the only "
                    "meaningful quantity is the before/after difference in the "
                    "SAME setup, so an absolute number with no baseline says "
                    "nothing")
        return (SCREEN_ONLY, "PRE_COMPLIANCE_ONLY",
                f"{identifier}: near-field / pre-compliance evidence, recorded "
                "against baseline "
                f"{record.get('baseline_id')!r} and NOT compared with the "
                "formal limit. emc-esd §8-9: a probe dBuV number cannot be "
                "compared with a laboratory limit, and a pre-compliance result "
                "is never labelled as formal certification. This does not "
                "close the row")

    required_class = _text(rule.get("required_compliance_class")).lower()
    if required_class:
        if required_class not in COMPLIANCE_CLASSES:
            return (INVALID_METRIC, "REQUIRED_CLASS_UNKNOWN",
                    f"{identifier}: the rule asks for compliance class "
                    f"{required_class!r}, which is not in "
                    f"{list(COMPLIANCE_CLASSES)}")
        if (COMPLIANCE_CLASSES.index(compliance)
                < COMPLIANCE_CLASSES.index(required_class)):
            return (CONFIRM, "EVIDENCE_CLASS_BELOW_REQUIREMENT",
                    f"{identifier}: the rule requires {required_class} "
                    f"evidence and this is {compliance}. Accredited-lab "
                    "compliance does not close in software; without the lab "
                    "artifact this is CONFIRM, not a signoff")
        if required_class == "accredited_lab" and not _text(
                record.get("lab_artifact")):
            return (CONFIRM, "LAB_ARTIFACT_ABSENT",
                    f"{identifier}: an accredited-lab row needs the lab's own "
                    "artifact reference (report id / test profile). Declaring "
                    "the class is not the same as holding the document")

    verdict, detail = judge(value, uncertainty, guard_band, limit)
    return (verdict, "" if verdict == GREEN else f"LIMIT_{verdict}",
            f"{identifier}: {detail}")


# --------------------------------------------------------------------------- #
# Rows, coverage, feedback
# --------------------------------------------------------------------------- #
def _stage_of(rule: dict) -> str:
    """The serial campaign stage a rule belongs to.

    An undeclared stage is placed LAST (``characterisation``) rather than
    first. That default can only delay the campaign from being called
    complete; the opposite default would let an unlabelled row make the
    power-safe stage look finished.
    """
    stage = _text(rule.get("campaign_stage")).lower()
    return stage if stage in CAMPAIGN_STAGES else "characterisation"


def build_rows(rules: Sequence[dict], records: Sequence[dict], unit: dict,
               raw_root: Path | None) -> list[dict]:
    by_rule: dict[str, list[dict]] = {}
    for record in records:
        by_rule.setdefault(_text(record.get("rule_id")), []).append(record)

    rows: list[dict] = []
    for rule in rules:
        rule_id = _text(rule.get("rule_id"))
        limit, limit_code, limit_detail = rule_limit(rule)
        attached = by_rule.get(rule_id, [])
        row: dict[str, Any] = {
            "rule_id": rule_id or "<unnamed>",
            "owner": _text(rule.get("owner")) or None,
            "required": bool(rule.get("required", True)),
            "campaign_stage": _stage_of(rule),
            "measurement_class": _text(rule.get("measurement_class")) or None,
            "limit": limit,
            "limit_authority": (limit or {}).get("authority"),
            "records": [],
        }

        if limit is None:
            row["verdict"] = NO_COVERAGE
            row["reason_code"] = limit_code
            row["verdict_reason"] = limit_detail
            # The records are still listed - they were taken, and hiding them
            # would make an unjudgeable row look like an unmeasured one.
            row["records"] = [{
                "measurement_id": _text(r.get("measurement_id")) or None,
                "verdict": NO_COVERAGE,
                "reason_code": limit_code,
                "detail": "recorded, not judged: " + limit_detail,
                "value": _number(r.get("value")),
                "units": _text(r.get("units")) or None,
            } for r in attached]
            rows.append(row)
            continue

        if not attached:
            row["verdict"] = NO_COVERAGE
            row["reason_code"] = "RECORDS_ABSENT"
            row["verdict_reason"] = (
                f"{rule_id}: the limit and its authority are present and no "
                "physical record was supplied. eligible > 0 with scanned = 0 "
                "is NO_COVERAGE - the row is open, not passed")
            rows.append(row)
            continue

        for record in attached:
            verdict, code, detail = validate_record(
                record, rule, limit, unit, raw_root)
            row["records"].append({
                "measurement_id": _text(record.get("measurement_id")) or None,
                "verdict": verdict,
                "reason_code": code or None,
                "detail": detail,
                "value": _number(record.get("value")),
                "units": _text(record.get("units")) or None,
                "uncertainty": _number(record.get("uncertainty")),
                "guard_band": _number(record.get("guard_band")),
                "compliance_class": _text(record.get("compliance_class")) or None,
                "instrument": {
                    "make": _text(record.get("instrument_make")) or None,
                    "model": _text(record.get("instrument_model")) or None,
                    "serial": _text(record.get("instrument_serial")) or None,
                    "calibration_id": _text(record.get("calibration_id")) or None,
                    "calibration_date": _text(record.get("calibration_date")) or None,
                },
                "raw_data_sha256": _text(record.get("raw_data_sha256")) or None,
                "baseline_id": _text(record.get("baseline_id")) or None,
                "assessor": _text(record.get("assessor")) or None,
                "timestamp": _text(record.get("timestamp")) or None,
            })

        row["verdict"] = worst([r["verdict"] for r in row["records"]])
        failing = [r for r in row["records"] if r["verdict"] == row["verdict"]]
        row["reason_code"] = failing[0]["reason_code"] if failing else None
        row["verdict_reason"] = failing[0]["detail"] if failing else ""
        rows.append(row)
    return rows


def campaign_order(rows: Sequence[dict]) -> dict:
    """How far the SERIAL bring-up campaign has actually got, plus any records
    taken out of order.

    The stages are serial for safety, not for bookkeeping: probing a functional
    node on a board whose rails were never proven safe is how a bring-up bench
    destroys the only unit it has. This is a ``domain_state``, published under
    its own key and NOT in ``gate.verdict`` - the closed verdict vocabulary is
    for gate decisions only (coverage-gate.md §"Verdict isim alani").
    """
    # Tri-state on purpose. "no_rows" is NOT "closed": a rule universe that
    # declares nothing for a stage has not proved that stage safe, it has said
    # nothing about it. Collapsing the two into a boolean would let an empty
    # universe read as a finished campaign - and it would equally let a
    # universe with a real power-safe row read as never started.
    state: dict[str, str] = {}
    for stage in CAMPAIGN_STAGES:
        in_stage = [r for r in rows if r["campaign_stage"] == stage]
        if not in_stage:
            state[stage] = "no_rows"
        elif all(r["verdict"] == GREEN for r in in_stage):
            state[stage] = "closed"
        else:
            state[stage] = "open"

    reached = "CAMPAIGN_COMPLETE"
    for stage in CAMPAIGN_STAGES:
        if state[stage] == "open":
            reached = stage.upper()
            break
    else:
        if all(value == "no_rows" for value in state.values()):
            reached = "NO_ROWS_DECLARED"
    if not any(row["records"] for row in rows):
        reached = "NOT_STARTED"

    out_of_order = []
    for index, stage in enumerate(CAMPAIGN_STAGES):
        if index == 0:
            continue
        earlier_open = [s for s in CAMPAIGN_STAGES[:index]
                        if state[s] == "open"]
        if not earlier_open:
            continue
        for row in rows:
            if row["campaign_stage"] == stage and row["records"]:
                out_of_order.append({
                    "rule_id": row["rule_id"], "stage": stage,
                    "blocked_by": earlier_open})
    return {
        "stages": state,
        "reached": reached,
        "out_of_order": out_of_order,
    }


def build_feedback(rows: Sequence[dict], unit: dict) -> list[dict]:
    """One L2 edge per row this gate could not close in the design's favour.

    The edge always names the rule's DECLARED owner. This tool never proposes
    the fix and never touches the board: the skill that owns the limit owns
    the response to a measurement that broke it.
    """
    actions = []
    for row in rows:
        if row["verdict"] not in (RED, STALE, INVALID_SPARAM, INVALID_METRIC):
            continue
        actions.append({
            "rule_id": row["rule_id"],
            "verdict": row["verdict"],
            "reason_code": row["reason_code"],
            "loop": "L2",
            "to_skill": row["owner"],
            "limit_authority": row["limit_authority"],
            "limit": row["limit"],
            "detail": row["verdict_reason"],
            "unit": {"board_revision": unit["board_revision"],
                     "board_hash": unit["board_hash"],
                     "serial": unit["serial"], "lot": unit["lot"]},
            "writes_board": False,
            "action": (
                f"L2 feedback to {row['owner']}: this gate measured the "
                f"physical result and cannot change the limit, the design or "
                f"the board. {row['owner']} owns the rule and owns the "
                "response; release stays blocked until the row is closed."),
        })
    return actions


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def build_report(rules: Sequence[dict], records: Sequence[dict], unit: dict,
                 argv: Sequence[str], raw_root: Path | None = None,
                 sources: dict | None = None) -> dict:
    rows = build_rows(rules, records, unit, raw_root)
    in_scope = [row for row in rows if row["required"]]
    optional = [row for row in rows if not row["required"]]

    eligible = len(in_scope)
    scanned = sum(1 for row in in_scope if row["verdict"] in JUDGED)
    violations = sum(1 for row in in_scope if row["verdict"] == RED)
    coverage = {
        "eligible": eligible,
        "scanned": scanned,
        "excluded": eligible - scanned,
        "violations": violations,
    }
    exclusions = [{"rule_id": row["rule_id"], "verdict": row["verdict"],
                   "reason_code": row["reason_code"],
                   "reason": row["verdict_reason"]}
                  for row in in_scope if row["verdict"] not in JUDGED]

    reason_codes = {row["reason_code"] for row in rows if row.get("reason_code")}

    status = worst([row["verdict"] for row in in_scope]) if in_scope \
        else NO_COVERAGE
    if not in_scope:
        reason_codes.add("NO_MANDATORY_ROWS")

    if coverage_rule_violations(coverage):
        # Cannot happen with the arithmetic above; if it ever does, the gate
        # says so instead of publishing a verdict resting on broken counts.
        status = INVALID_COVERAGE
        reason_codes.add("INVALID_COVERAGE")

    if not unit["accepted"]:
        status = RED
        reason_codes.add("ACCEPTED_UNIT_PRECONDITION_BROKEN")

    order = campaign_order(rows)
    if order["out_of_order"]:
        reason_codes.add("CAMPAIGN_ORDER_VIOLATION")
    if raw_root is None:
        reason_codes.add("RAW_DATA_NOT_VERIFIED")

    # The acceptance axis, computed with the shared helper so "a threshold
    # nobody stated cannot have been met" is not re-implemented here. The
    # PASS/FAIL it returns is mapped onto the closed verdict vocabulary
    # (coverage-gate.md) before it is published as gate.verdict.
    applied = any(row["limit"] is not None for row in in_scope)
    gate = acceptance_verdict(
        applied=applied,
        met=bool(in_scope) and all(row["verdict"] == GREEN for row in in_scope),
        computed=unit["accepted"] and scanned > 0,
        missing_reason=(
            "no D row arrived with a limit AND an authority binding, so there "
            "is nothing this gate is entitled to judge against"
            if not applied else
            "the accepted-unit precondition is broken, or no row was actually "
            "measured"))
    gate_verdict = {"PASS": GREEN, "FAIL": RED,
                    "NO_COVERAGE": NO_COVERAGE}[gate["verdict"]]
    gate_block: dict[str, Any] = {
        "applied": applied,
        "verdict": gate_verdict,
        "rows_green": sum(1 for row in in_scope if row["verdict"] == GREEN),
        "rows_required": eligible,
    }
    if gate.get("verdict_reason"):
        gate_block["verdict_reason"] = gate["verdict_reason"]

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {
            "name": TOOL_NAME,
            "module": MODULE_NAME,
            "version": TOOL_VERSION,
            "sha256": sha256_file(SELF),
            "argv": [LOCATOR, *[str(a) for a in argv]],
            "exit_code": status_exit_code(status),
        },
        "gate_id": GATE_ID,
        "signoff": False,
        "generated_utc": _now_utc(),
        "unit": unit,
        "sources": sources or {},
        "evidence_origin": [PHYSICAL, "TOOL_REPORT", "DESIGN_ARTIFACT",
                            "AUTHORITATIVE_SOURCE"],
        # Order matches skills/_shared/coverage-gate.md, which names this
        # skill as its worked "produces-evidence-for L3" example.
        "assurance_loop": ["L2", "L3"],
        "rows": rows,
        "optional_rows": [row["rule_id"] for row in optional],
        "coverage": coverage,
        "exclusions": exclusions,
        "campaign": order,
        "domain_state": order["reached"],
        "gate": gate_block,
        "status": status,
        "effective_status": status,
        "reason_codes": sorted(reason_codes),
        "limits": LIMITS,
    }
    return report


def build_receipt(report: dict, manifest_path: Path | None,
                  feedback_count: int) -> dict:
    manifest = {"path": str(manifest_path) if manifest_path else None,
                "sha256": (sha256_file(manifest_path)
                           if manifest_path and Path(manifest_path).is_file()
                           else None)}
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "gate_id": GATE_ID,
        "verdict": report["status"],
        "signoff": False,
        "generated_utc": report["generated_utc"],
        "tool": report["tool"],
        "unit": report["unit"],
        "manifest": manifest,
        "assurance_loop": report["assurance_loop"],
        "evidence_origin": report["evidence_origin"],
        "coverage": report["coverage"],
        "domain_state": report["domain_state"],
        "rows": [{"rule_id": row["rule_id"], "owner": row["owner"],
                  "required": row["required"], "verdict": row["verdict"],
                  "reason_code": row["reason_code"],
                  "limit_authority": row["limit_authority"]}
                 for row in report["rows"]],
        "exclusions": report["exclusions"],
        "reason_codes": report["reason_codes"],
        "feedback_actions": feedback_count,
        "limits": report["limits"],
    }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
_UNIT = {
    "board_revision": "B",
    "board_hash": "a" * 64,
    "serial": "SN-0001",
    "lot": "LOT-2026-31",
    "first_article": {"path": "fai.json", "sha256": None, "verdict": "GREEN"},
    "accepted": True,
}

_AUTHORITY = {"owner_skill": "empedans-stackup",
              "document_id": "stackup.json@rev3",
              "locator": "artifacts/stackup.json",
              "accessed": "2026-08-01"}


def _rule(**overrides) -> dict:
    rule = {
        "rule_id": "IMP-DIFF-90",
        "owner": "empedans-stackup",
        "required": True,
        "measurement_class": "tdr",
        "campaign_stage": "characterisation",
        "limit": {"units": "ohm", "min": 81.0, "max": 99.0,
                  "authority": dict(_AUTHORITY)},
    }
    rule.update(overrides)
    return rule


def _record(**overrides) -> dict:
    record = {
        "measurement_id": "M-001",
        "rule_id": "IMP-DIFF-90",
        "evidence_origin": PHYSICAL,
        "measurement_class": "tdr",
        "board_revision": "B",
        "board_hash": "a" * 64,
        "serial": "SN-0001",
        "lot": "LOT-2026-31",
        "instrument_make": "Tektronix",
        "instrument_model": "DSA8300",
        "instrument_serial": "TK-9",
        "calibration_id": "CAL-2026-07-14",
        "calibration_date": "2026-07-14",
        "method": "TDR on impedance coupon, 35 ps rise",
        "setup": "SMA launch, 50 ohm, 2 m phase-stable cable",
        "reference_plane": "coupon launch pad, after SOLT",
        "measurement_zone": "coupon centre 20-40 mm",
        "environment": "23 C, 45 %RH",
        "load_state": "unpowered coupon",
        "baseline_id": "BL-COUPON-1",
        "port_map": [{"port": 1, "net": "COUPON_P"},
                     {"port": 2, "net": "COUPON_N"}],
        "reference_impedance": 50.0,
        "fixture": "SMA end-launch, torque 0.56 Nm",
        "deembed_method": "SOLT to launch pad; fixture de-embedded",
        "value": 90.0,
        "units": "ohm",
        "uncertainty": 1.5,
        "guard_band": 1.0,
        "raw_data_sha256": "b" * 64,
        "compliance_class": "bench",
        "assessor": "lab@example.org",
        "timestamp": "2026-08-10T09:00:00Z",
    }
    record.update(overrides)
    return record


def run_self_tests() -> None:
    """Every case here has an answer known before the tool is run."""

    def a_valid_measurement_inside_the_limit_is_green() -> None:
        report = build_report([_rule()], [_record()], _UNIT, [])
        assert report["status"] == GREEN, report["status"]
        assert report["coverage"] == {"eligible": 1, "scanned": 1,
                                      "excluded": 0, "violations": 0}

    def outside_the_limit_is_red_with_an_owner() -> None:
        report = build_report([_rule()], [_record(value=120.0)], _UNIT, [])
        assert report["status"] == RED, report["status"]
        actions = build_feedback(report["rows"], _UNIT)
        assert actions and actions[0]["to_skill"] == "empedans-stackup", actions

    def a_row_with_no_records_is_never_green() -> None:
        report = build_report([_rule()], [], _UNIT, [])
        assert report["status"] == NO_COVERAGE, report["status"]
        assert report["coverage"]["scanned"] == 0

    def a_row_with_no_limit_is_never_green() -> None:
        rule = _rule()
        rule.pop("limit")
        report = build_report([rule], [_record()], _UNIT, [])
        assert report["status"] == NO_COVERAGE, report["status"]
        assert "LIMIT_NOT_SUPPLIED" in report["reason_codes"]

    def a_foreign_board_hash_is_stale() -> None:
        report = build_report([_rule()], [_record(board_hash="c" * 64)],
                              _UNIT, [])
        assert report["status"] == STALE, report["status"]

    def an_uncalibrated_record_is_no_coverage() -> None:
        record = _record()
        record["calibration_id"] = ""
        report = build_report([_rule()], [record], _UNIT, [])
        assert report["status"] == NO_COVERAGE, report["status"]

    def a_straddling_interval_is_inconclusive() -> None:
        report = build_report([_rule()], [_record(value=98.5, uncertainty=1.0,
                                                  guard_band=1.0)], _UNIT, [])
        assert report["status"] == INCONCLUSIVE, report["status"]

    def a_touchstone_without_port_map_is_invalid_sparam() -> None:
        record = _record()
        record.pop("port_map")
        report = build_report([_rule()], [record], _UNIT, [])
        assert report["status"] == INVALID_SPARAM, report["status"]

    def pre_compliance_never_becomes_green() -> None:
        rule = _rule(rule_id="EMC-RE", owner="emc-esd",
                     measurement_class="near_field",
                     limit={"units": "dBuV/m", "max": 40.0,
                            "authority": dict(_AUTHORITY,
                                              owner_skill="emc-esd")})
        record = _record(rule_id="EMC-RE", measurement_class="near_field",
                         units="dBuV/m", value=12.0,
                         compliance_class="pre_compliance")
        report = build_report([rule], [record], _UNIT, [])
        assert report["status"] == SCREEN_ONLY, report["status"]

    def a_self_authored_limit_is_refused() -> None:
        rule = _rule(limit={"units": "ohm", "min": 81.0, "max": 99.0,
                            "authority": dict(_AUTHORITY,
                                              owner_skill=GATE_ID)})
        report = build_report([rule], [_record()], _UNIT, [])
        assert "SELF_AUTHORED_LIMIT" in report["reason_codes"], report

    def coverage_arithmetic_always_holds() -> None:
        for rules, records in (([_rule()], [_record()]),
                               ([_rule()], []),
                               ([_rule()], [_record(value=120.0)]),
                               ([], [])):
            report = build_report(rules, records, _UNIT, [])
            assert coverage_rule_violations(report["coverage"]) == [], report

    tests = (
        ("inside the limit is GREEN", a_valid_measurement_inside_the_limit_is_green),
        ("outside the limit is RED with an owner", outside_the_limit_is_red_with_an_owner),
        ("no record is never GREEN", a_row_with_no_records_is_never_green),
        ("no limit is never GREEN", a_row_with_no_limit_is_never_green),
        ("a foreign board hash is STALE", a_foreign_board_hash_is_stale),
        ("uncalibrated is NO_COVERAGE", an_uncalibrated_record_is_no_coverage),
        ("a straddling interval is INCONCLUSIVE", a_straddling_interval_is_inconclusive),
        ("incomplete S-parameter metadata is INVALID_SPARAM", a_touchstone_without_port_map_is_invalid_sparam),
        ("pre-compliance stays SCREEN_ONLY", pre_compliance_never_becomes_green),
        ("a self-authored limit is refused", a_self_authored_limit_is_refused),
        ("coverage arithmetic always holds", coverage_arithmetic_always_holds),
    )
    for name, test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"SELF-TEST FAIL [{name}]: {exc}", file=sys.stderr)
            raise
        else:
            print(f"SELF-TEST PASS [{name}]")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rules", type=Path,
                        help="the D-row universe WITH limits and authority "
                             "bindings (dogrulama-matrisi). No limit here "
                             "means NO_COVERAGE there; nothing is defaulted")
    parser.add_argument("--records", type=Path,
                        help="the lab measurement records to ingest")
    parser.add_argument("--accepted-unit", type=Path, dest="accepted_unit",
                        help="first_article_report.json from "
                             "ilk-urun-montaj-kabulu: the unit these "
                             "measurements are allowed to be about")
    parser.add_argument("--raw-root", type=Path, dest="raw_root",
                        help="directory the records' raw_data_path values "
                             "resolve against. Given it, every declared "
                             "raw_data_sha256 is verified against the file")

    out = parser.add_argument_group("outputs")
    out.add_argument("--json", type=Path, dest="json_path",
                     help="write the manifest report here")
    out.add_argument("--manifest", type=Path, dest="manifest_path",
                     help="physical_measurement_manifest.json")
    out.add_argument("--receipt", type=Path, dest="receipt_path",
                     help="l3_gate_receipt.json")
    out.add_argument("--feedback", type=Path, dest="feedback_path",
                     help="l3_feedback_actions.json")

    parser.add_argument("--self-test", dest="self_test", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        try:
            run_self_tests()
        except AssertionError as exc:
            diag(f"SELF-TEST FAILED: {exc}")
            return 1
        return 0

    missing = [name for name, value in (("--rules", args.rules),
                                        ("--records", args.records),
                                        ("--accepted-unit", args.accepted_unit))
               if value is None]
    if missing:
        parser.error(
            f"missing {', '.join(missing)}. None of the three is optional: "
            "without the rule universe there is nothing to close, without the "
            "records there is no evidence, and without the accepted unit the "
            "evidence is about no particular board")

    try:
        unit = accepted_unit(load_json(args.accepted_unit, "accepted unit"),
                             Path(args.accepted_unit))
        rules = _listify(load_json(args.rules, "rule universe"), "rules",
                         "rule universe")
        records = _listify(load_json(args.records, "records"), "records",
                           "measurement records")
    except MeasurementIngestError as exc:
        emit(error_report(SCHEMA_VERSION, "INPUT_UNUSABLE", str(exc)))
        diag(f"ERROR: {exc}")
        return EXIT_ERROR

    sources = {
        "rules": {"path": str(args.rules), "sha256": sha256_file(args.rules)},
        "records": {"path": str(args.records),
                    "sha256": sha256_file(args.records)},
        "accepted_unit": {"path": str(args.accepted_unit),
                          "sha256": sha256_file(args.accepted_unit)},
        "raw_root": str(args.raw_root) if args.raw_root else None,
    }
    report = build_report(rules, records, unit, argv,
                          raw_root=args.raw_root, sources=sources)
    actions = build_feedback(report["rows"], unit)

    manifest_path = args.manifest_path or args.json_path
    if args.json_path:
        write_report(report, args.json_path)
    if args.manifest_path and args.manifest_path != args.json_path:
        write_report(report, args.manifest_path)
    if args.feedback_path:
        write_report({"schema_version": FEEDBACK_SCHEMA_VERSION,
                      "gate_id": GATE_ID,
                      "generated_utc": report["generated_utc"],
                      "unit": report["unit"],
                      "actions": actions,
                      "writes_board": False}, args.feedback_path)
    if args.receipt_path:
        write_report(build_receipt(report, manifest_path, len(actions)),
                     args.receipt_path)

    diag("")
    diag(f"physical measurement: {len(report['rows'])} rule row(s), "
         f"{sum(len(r['records']) for r in report['rows'])} record(s) "
         f"on {unit['board_revision']}/{unit['serial']}/{unit['lot']}")
    diag(f"  board {unit['board_hash'][:12]}...  "
         f"first article {unit['first_article']['verdict']}")
    for row in report["rows"]:
        diag(f"  {row['verdict']:15s} {row['rule_id']:24s} "
             f"owner={row['owner']}")
    if not unit["accepted"]:
        diag("  the first article was NOT accepted: an electrical fault hunt "
             "on an unaccepted unit charges build defects to the design")
    for action in actions:
        diag(f"  L2 -> {action['to_skill']}: {action['rule_id']}")
    diag(f"  coverage: {report['coverage']}")
    diag(f"  domain_state: {report['domain_state']}   "
         f"gate.verdict: {report['gate']['verdict']}")
    diag(f"  status: {report['status']}")

    emit(report)
    return status_exit_code(report["status"])


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
