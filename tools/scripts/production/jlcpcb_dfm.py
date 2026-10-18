#!/usr/bin/env python3
"""JLCPCB early-DFM API bridge: submit a fabrication package for a
manufacturer-side manufacturability check, ahead of local DFM screening.

backend: cli  (network: a JLCPCB DFM endpoint; opt-in only)

STATUS: CONFIRM - SCAFFOLD, NOT VERIFIED AGAINST A REAL JLCPCB ACCOUNT
-------------------------------------------------------------------------
The archive's own version of this bridge called itself "bilerek bir ISKELET
+ YER TUTUCU" (deliberately a skeleton + placeholder): it could not confirm
that JLCPCB publishes a public, "analysis only, no order" DFM endpoint
separate from its pricing/ordering API, and the request/response shape was
never seen. That uncertainty has NOT been resolved in this port either - no
outbound network access to a JLCPCB account was available while writing this
file. Nothing here is invented to look more finished than it is:

  * the endpoint URL and API key are both read from environment variables
    (``JLCPCB_DFM_ENDPOINT`` / ``JLCPCB_API_KEY``) - NEITHER is hardcoded,
    because a wrong hardcoded endpoint would be worse than requiring one;
  * ``parse_dfm_response`` documents its assumed response shape as a GUESS,
    clearly separated from the request-building code that does not guess;
  * every path that would need real API behaviour to verify raises
    ``JlcpcbDfmUnavailable`` (NO_COVERAGE) rather than fabricate a result.

WHEN YOU HAVE REAL API ACCESS: point ``JLCPCB_DFM_ENDPOINT`` at the real
endpoint, adjust ``build_submission_payload``/``parse_dfm_response`` to the
real request/response shape (both are isolated on purpose - this is the
ONLY code that should need to change), and this module's CLI contract
(exit codes, JSON report shape, --self-test) does not need to change at all.

CREDENTIAL / ENDPOINT INJECTION, NEVER HARDCODED
--------------------------------------------------
Both must be present or the request is never sent - a request half-built
from a missing key is not an acceptable failure mode for something that
uploads a design to a third party.

OFFLINE / MISSING-CONFIG BEHAVIOUR IS FAIL-CLOSED
----------------------------------------------------
No key, no endpoint, no network, a non-200 response, or an unparsable
response all produce NO_COVERAGE with an explanatory detail - never a
fabricated PASS. This mirrors ``scripts/production/freerouting.py``'s
jar-bootstrap discipline: a network call with a real external side effect
(here: your design leaves your machine) never fires unless the caller
supplied real configuration, and a caller must pass ``--allow-network``
even when configuration IS present (belt and suspenders: configuration
existing is not the same as consenting to send data this run).

Usage:
    jlcpcb_dfm.py <gerber_zip> --allow-network [--json out.json]
                  [--timeout-s 120]
    jlcpcb_dfm.py --self-test

Exit: 0 = DFM check ran and reported clean, 1 = DFM check ran and found
issues (or NO_COVERAGE - see the report's status field), 2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import FAIL, Finding, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "jlcpcb-dfm-report/v1"
TOOL_NAME = "jlcpcb_dfm"

API_KEY_ENV = "JLCPCB_API_KEY"
ENDPOINT_ENV = "JLCPCB_DFM_ENDPOINT"


class JlcpcbDfmUnavailable(RuntimeError):
    """Configuration is missing, or the network call could not be trusted.

    A distinct type so a caller can tell "this DFM check did not run" apart
    from an unrelated failure and mark the step NEEDS_HUMAN / NO_COVERAGE
    instead of guessing.
    """


def api_key() -> str | None:
    return os.environ.get(API_KEY_ENV) or None


def api_endpoint() -> str | None:
    return os.environ.get(ENDPOINT_ENV) or None


@dataclass
class DfmViolation:
    code: str
    message: str
    severity: str = "warning"   # "error" | "warning" - GUESSED, see module docstring


@dataclass
class DfmResult:
    ok: bool | None
    """The vendor's own verdict. ``None`` means the response did not carry
    one - NOT that it passed. It used to be inferred from an empty issue
    list, which is the "absence read as zero" bug: a response holding only
    ``issues: []`` says nothing about whether the vendor accepted the
    board."""

    violations: list[DfmViolation] = field(default_factory=list)
    raw_response: dict = field(default_factory=dict)


def build_submission_payload(gerber_zip_path: str) -> dict:
    """The request body this module WOULD send.

    GUESSED SHAPE, not verified: a filename plus a placeholder for however
    the real API wants the file transferred (multipart upload vs a
    pre-signed URL vs base64 - unknown without real API docs). Isolated in
    its own function so fixing it against the real API touches nothing else
    in this module.
    """
    return {
        "filename": Path(gerber_zip_path).name,
        "size_bytes": Path(gerber_zip_path).stat().st_size,
    }


def parse_dfm_response(raw: dict) -> DfmResult:
    """Turn a parsed JSON response into a ``DfmResult``.

    GUESSED SHAPE (see the module docstring): assumes a top-level ``"pass"``
    boolean and an ``"issues"`` list of ``{"code", "message", "severity"}``
    objects. An unrecognised shape is NOT a crash and NOT a silent pass - it
    raises ``JlcpcbDfmUnavailable`` so the caller reports NO_COVERAGE, the
    same fail-closed rule ``common.decisions``/``common.findings`` use
    everywhere else in this bundle for an unrecognised schema.
    """
    if "pass" not in raw and "issues" not in raw:
        raise JlcpcbDfmUnavailable(
            "the DFM response has neither 'pass' nor 'issues' - this is not "
            "the assumed (unverified) response shape; parse_dfm_response "
            "must be updated against the real API response before this "
            "result can be trusted")
    violations = [
        DfmViolation(code=i.get("code", "UNKNOWN"),
                    message=i.get("message", ""),
                    severity=i.get("severity", "warning"))
        for i in raw.get("issues", [])
    ]
    ok = bool(raw["pass"]) if "pass" in raw else None
    return DfmResult(ok=ok, violations=violations, raw_response=raw)


def submit_for_dfm(gerber_zip_path: str, timeout_s: int = 120) -> DfmResult:
    """POST the fabrication package to the configured JLCPCB DFM endpoint.

    Fail-closed on every axis: missing file, missing key, missing endpoint,
    network error, non-200, or an unrecognised response all raise
    ``JlcpcbDfmUnavailable`` - never a fabricated ``DfmResult``.
    """
    if not Path(gerber_zip_path).is_file():
        raise JlcpcbDfmUnavailable(
            f"submit_for_dfm: {gerber_zip_path} does not exist")

    key = api_key()
    endpoint = api_endpoint()
    if not key or not endpoint:
        missing = ", ".join(
            name for name, value in ((API_KEY_ENV, key), (ENDPOINT_ENV, endpoint))
            if not value)
        raise JlcpcbDfmUnavailable(
            f"submit_for_dfm: {missing} not set. No request was sent; a "
            "design is never uploaded to a third party from partial "
            "configuration.")

    import urllib.error
    import urllib.request

    payload = json.dumps(build_submission_payload(gerber_zip_path)).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "User-Agent": "pcb-agent-jlcpcb-dfm-bridge"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                raise JlcpcbDfmUnavailable(
                    f"submit_for_dfm: HTTP {response.status} from {endpoint}")
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise JlcpcbDfmUnavailable(
            f"submit_for_dfm: could not reach {endpoint}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JlcpcbDfmUnavailable(
            f"submit_for_dfm: {endpoint} did not return valid JSON: {exc}") from exc

    return parse_dfm_response(raw)


def check_dfm(gerber_zip_path: str, allow_network: bool,
              timeout_s: int = 120) -> Finding:
    """``submit_for_dfm`` wrapped as a Finding. NO_COVERAGE whenever the
    check did not really run - configuration missing, network disabled, or
    the response could not be trusted - never a PASS standing in for that."""
    if not allow_network:
        return result(
            "jlcpcb_dfm", 0, [],
            "network call not requested (pass --allow-network) - the "
            "design was NOT submitted anywhere")
    try:
        outcome = submit_for_dfm(gerber_zip_path, timeout_s)
    except JlcpcbDfmUnavailable as exc:
        return result("jlcpcb_dfm", 0, [], str(exc))

    unverified = ("response shape is UNVERIFIED against a real JLCPCB "
                  "account - see the module docstring before trusting this")
    violations = [
        {"code": v.code, "message": v.message, "severity": v.severity}
        for v in outcome.violations]

    # ``outcome.ok`` is the vendor's own verdict and it used to be dropped on
    # the floor here: check_dfm read only the violation list, so a response
    # saying {"pass": false, "issues": []} - declined, with no itemised
    # reason - came back as a PASS finding with scanned=1.
    if outcome.ok is None and not violations:
        return result(
            "jlcpcb_dfm", 0, [],
            "the DFM response carried no 'pass' verdict and no issues, so it "
            f"says nothing about this board. {unverified}")
    if outcome.ok is False and not violations:
        violations = [{
            "code": "VENDOR_DECLINED",
            "message": "the DFM response reported pass=false but listed no "
                       "issues. The vendor did not accept the board; the "
                       "reason is not in the response.",
            "severity": "error",
        }]
    return result("jlcpcb_dfm", 1, violations, unverified)


def self_test() -> list[str]:
    """Pure logic. Does NOT touch the network - the real endpoint has never
    been exercised in this environment (see the module docstring)."""
    problems: list[str] = []

    # A recognised, clean response.
    clean = parse_dfm_response({"pass": True, "issues": []})
    if not clean.ok:
        problems.append("a clean response was not read as ok")

    # A recognised, dirty response.
    dirty = parse_dfm_response({"pass": False, "issues": [
        {"code": "SILK_OVER_PAD", "message": "silkscreen over exposed pad",
         "severity": "warning"}]})
    if dirty.ok or not dirty.violations:
        problems.append("a dirty response did not carry its violation through")

    # FAULT INJECTION: an unrecognised response shape must be refused, not
    # silently read as a pass.
    try:
        parse_dfm_response({"unexpected": "shape"})
    except JlcpcbDfmUnavailable:
        pass
    else:
        problems.append(
            "fault injection did not hold: an unrecognised response shape "
            "was accepted")

    # FAULT INJECTION: missing configuration must fail closed without ever
    # attempting a network call.
    old_key, old_endpoint = os.environ.pop(API_KEY_ENV, None), os.environ.pop(ENDPOINT_ENV, None)
    try:
        submit_for_dfm(__file__)   # a real file, so only the config check is exercised
    except JlcpcbDfmUnavailable:
        pass
    else:
        problems.append(
            "fault injection did not hold: submit_for_dfm proceeded with "
            "no key/endpoint configured")
    finally:
        if old_key is not None:
            os.environ[API_KEY_ENV] = old_key
        if old_endpoint is not None:
            os.environ[ENDPOINT_ENV] = old_endpoint

    # allow_network=False must never touch submit_for_dfm at all.
    finding = check_dfm("nonexistent.zip", allow_network=False)
    if finding["status"] != "NO_COVERAGE":
        problems.append(
            "check_dfm ran (or claimed to run) without --allow-network")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gerber_zip", nargs="?",
                        help="path to the fabrication package (zip)")
    parser.add_argument("--allow-network", action="store_true",
                        help="actually submit to the configured endpoint "
                             "(opt-in: uploads your design to a third party)")
    parser.add_argument("--timeout-s", type=int, default=120)
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: jlcpcb_dfm")
        return 0

    if not args.gerber_zip:
        parser.error("the gerber package path is required")

    finding = check_dfm(args.gerber_zip, args.allow_network, args.timeout_s)
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "gerber_zip": args.gerber_zip,
        "status": finding["status"],
        "checks": [finding],
        "summary": {"PASS": int(finding["status"] == "PASS"),
                   "FAIL": int(finding["status"] == FAIL),
                   "NO_COVERAGE": int(finding["status"] == "NO_COVERAGE")},
    }
    write_report(report, args.json)
    emit(report)
    return 1 if finding["status"] != "PASS" else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
