#!/usr/bin/env python3
"""One command for fabrication outputs - with the verification gate in front.

Runs DRC + ERC (+ the artifact contracts) and only then hands the board to
KiBot. The point is that "one-click gerbers" must not quietly become "one-click
gerbers with the DRC skipped".

backend: cli  (external binaries: kicad-cli, kibot)

FAIL-CLOSED IN THREE PLACES
---------------------------
1. An UNRECOGNISED DRC/ERC report schema is a FAIL, not a pass. ``violations``
   and ``sheets`` are the two known shapes; anything else means nothing could
   be read, and a "clean" verdict from an unreadable report is not clean.
2. A MISSING pcbnew is a FAIL by default. An earlier version caught the import
   error, prefixed the message with "WARNING:", and then computed its verdict
   by FILTERING OUT messages starting with "WARNING:" - so the real board
   checks could fail to run entirely and the gate still passed.
   ``--skip-board-checks`` is now required to proceed, and even then the result
   is NEEDS_HUMAN, never PASS.
3. A MISSING kibot is an error, not an empty success.
4. An UNMEASURED check shuts the gate. The verdict is the monotone rollup from
   ``common/findings.rollup_status``: PASS only when every in-scope check was
   measured and clean. It used to be ``not any(FAIL)``, so NO_COVERAGE and
   PASS opened the gate alike and ``--skip-verification``, ``--skip-board-
   checks`` and "no contracts supplied" all produced gerbers off an unmeasured
   gate.

``--skip-verification`` exists only for debugging: it skips DRC/ERC and leaves
the gate at NO_COVERAGE, so no fabrication output is produced. Nothing in this
tool produces outputs off a gate that did not measure.

A check that genuinely does not apply to a revision is taken out of scope
explicitly with ``--exclude CHECK=REASON``; the reason lands in the report.
Only an unmeasured check can be excluded - a measured FAIL shuts the gate
however it is labelled.

Usage:
    outputs.py <board.kicad_pcb> <schematic.kicad_sch>
               [--out-dir production] [--kibot-config kibot.yaml]
               [--parts-contract parts.json] [--drc-contract drc.json]
               [--exclude CHECK=REASON]
               [--skip-board-checks] [--skip-verification] [--json out.json]
    outputs.py --self-test

Exit: 0 = gate clean (and KiBot ran, if requested), 1 = gate shut (FAIL *or*
NO_COVERAGE), 2 = error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    PASS,
    Finding,
    board_report,
    result,
    rollup_status,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.production.contracts import (  # noqa: E402
    ContractGateError,
    build_drc_contract,
    drc_gate_passes,
    parts_gate_status,
    schema_unrecognised,
    write_drc_contract,
)

SCHEMA_VERSION = "production-gate-report/v1"
TOOL_NAME = "production_outputs"

KIBOT_TEMPLATE = """\
kibot:
  version: '1'

outputs:
  - name: 'gerbers'
    comment: 'Fabrication gerbers'
    type: gerber
    dir: 'gerbers'
    layers: 'all'

  - name: 'drill'
    comment: 'Drill files'
    type: excellon
    dir: 'gerbers'

  - name: 'position'
    comment: 'Pick and place (CPL/POS)'
    type: position
    dir: 'pnp'
    options:
      format: CSV
      units: millimeters

  - name: 'bom'
    comment: 'Human-readable BOM'
    type: bom
    dir: 'bom'
    options:
      format: XLSX

  - name: 'package'
    comment: 'Everything in one archive'
    type: compress
    dir: '.'
    options:
      files:
        - from_output: gerbers
        - from_output: drill
        - from_output: position
        - from_output: bom
"""


@dataclass
class KiBotRun:
    ok: bool
    out_dir: str
    stdout: str
    stderr: str


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def run_drc(board: str, out: str, cli: str | None = None) -> dict:
    """``kicad-cli pcb drc --format json``. Returns the parsed report.

    Exit 0 (clean) and the violation exit codes are both normal; anything else
    is a real tool failure.

    ``--all-track-errors`` is NOT optional and was missing here until
    2026-08-10. ``drc_gate.py::build_argv`` documents why with the measurement
    that found it: without the flag kicad-cli reports only the FIRST error on
    each track, and which one is first depends on whichever DRC worker
    finishes first, so the same board with the same digest and the same argv
    produced 189..205 violations across five runs instead of a stable 292.
    That fix landed in drc_gate.py and not here, and this is the DRC that
    decides whether gerbers are written - so the fabrication gate kept
    measuring a partial, run-dependent view of the board.
    """
    from common.kicad_cli import KicadCliError, _require_cli
    exe = _require_cli(cli)
    proc = subprocess.run([exe, "pcb", "drc", "--format", "json",
                           "--severity-all", "--all-track-errors",
                           "-o", out, board],
                          capture_output=True, text=True)
    if not Path(out).is_file():
        raise KicadCliError(f"pcb drc produced no report: {proc.stderr}")
    return json.loads(Path(out).read_text(encoding="utf-8"))


def run_erc(schematic: str, out: str, cli: str | None = None) -> dict:
    from common.kicad_cli import KicadCliError, _require_cli
    exe = _require_cli(cli)
    proc = subprocess.run([exe, "sch", "erc", "--format", "json",
                           "--severity-all", "-o", out, schematic],
                          capture_output=True, text=True)
    if not Path(out).is_file():
        raise KicadCliError(f"sch erc produced no report: {proc.stderr}")
    return json.loads(Path(out).read_text(encoding="utf-8"))


def design_census(subject: str, source: str) -> int:
    """How many objects the DRC/ERC actually had to look at.

    ``drc_gate.py`` has always counted this and refuses to report PASS on a
    zero-object design; this file did not, and passed ``scanned=1`` as a
    constant. A board with no copper produces a clean kicad-cli report, so
    "no violations" and "nothing to violate" were the same finding here - in
    the gate that decides whether gerbers get written. The census functions
    live in drc_gate.py rather than being re-implemented, so the two gates
    cannot drift apart on what counts as an object.
    """
    from scripts.drc_gate import pcb_census, sch_census_set, sheet_set

    path = Path(subject)
    if not path.is_file():
        return 0
    if source == "drc":
        census = pcb_census(path.read_text(encoding="utf-8", errors="replace"))
    else:
        census = sch_census_set(sheet_set(path))
    return sum(census.values())


def check_report_gate(raw: dict, source: str, subject: str,
                      scanned: int) -> Finding:
    """One DRC or ERC report reduced to a finding, fail-closed on a bad schema.

    ``scanned`` is the object census from ``design_census`` and is explicit,
    not defaulted: a default is what let this read ``1`` forever.
    """
    if schema_unrecognised(raw):
        return Finding(
            f"{source}_gate", FAIL, 1,
            [{"problem": "SCHEMA_UNRECOGNISED",
              "message": f"the {source.upper()} report has neither 'violations' "
                         f"nor 'sheets'. Fail-closed: no fabrication output. The "
                         f"kicad-cli version may have changed and the report "
                         f"format must be re-verified."}],
            f"{source} report schema check")
    contract = build_drc_contract(subject, raw, source)
    violations = [] if contract.violation_count == 0 else [{
        "problem": "VIOLATIONS_PRESENT",
        "count": contract.violation_count,
        "first": contract.violations[:5],
    }]
    return result(f"{source}_gate", scanned, violations,
                  f"{contract.violation_count} error-severity, "
                  f"{contract.warning_count} warning-severity, "
                  f"{scanned} object(s) in the design")


def check_board_checks(board: str, skip: bool) -> Finding:
    """The pcbnew-backed board checks, or an explicit refusal.

    Never a silent pass: without pcbnew the mask-dam / via-in-pad / annular
    ring / stitch checks did not run at all.
    """
    try:
        import pcbnew  # noqa: F401
    except ImportError:
        if skip:
            return Finding(
                "board_checks", "NO_COVERAGE", 0, [],
                "NEEDS_HUMAN (--skip-board-checks was given deliberately): "
                "pcbnew is unavailable, so the mask-dam / via-in-pad / annular "
                "ring / stitch checks did NOT run. This is not a PASS.")
        return Finding(
            "board_checks", FAIL, 1,
            [{"problem": "PCBNEW_MISSING",
              "message": "pcbnew is unavailable, so the real board checks could "
                         "not run. Default behaviour is fail-closed: no "
                         "fabrication output. Install pcbnew, or pass "
                         "--skip-board-checks deliberately (the result is then "
                         "NEEDS_HUMAN, still not a PASS)."}],
            "pcbnew availability")
    return result("board_checks", 1, [],
                  "pcbnew is available; run scripts/board/*.py for the detailed "
                  "findings - this gate only records that they are runnable")


def check_contract_gates(parts_contract: str | None,
                         drc_contract: str | None) -> Finding:
    """The on-disk artifact contracts, when the caller supplies them.

    A supplied contract that examined nothing (``rows: []``) is NOT folded
    into the clean count. It leaves the finding at NO_COVERAGE even when the
    other contract passed, because "one of the two gates measured nothing" is
    not a state this check may report as PASS.
    """
    checks = 0
    unmeasured: list[str] = []
    violations: list[dict] = []
    if parts_contract:
        try:
            status, issues, _ = parts_gate_status(parts_contract)
            if status == "NO_COVERAGE":
                unmeasured.append(f"{parts_contract} lists no parts")
            else:
                checks += 1
                if status == FAIL:
                    violations.append({"contract": parts_contract,
                                       "issues": issues})
        except ContractGateError as exc:
            checks += 1
            violations.append({"contract": parts_contract, "issues": [str(exc)]})
    if drc_contract:
        checks += 1
        try:
            if not drc_gate_passes(drc_contract):
                violations.append({"contract": drc_contract,
                                   "issues": ["violation_count != 0"]})
        except ContractGateError as exc:
            violations.append({"contract": drc_contract, "issues": [str(exc)]})
    detail = ("parts.json and drc.json are re-read from disk at the gate, "
              "never taken from an in-memory value")
    if unmeasured:
        # NO_COVERAGE with scanned > 0: partially covered. Finding allows this
        # (it only forbids the reverse, scanned == 0 with a non-NO_COVERAGE
        # status), and it is the honest description of a mixed result.
        return Finding("artifact_contracts",
                       FAIL if violations else "NO_COVERAGE",
                       checks, violations,
                       f"{detail}; NOT MEASURED: {'; '.join(unmeasured)}")
    return result("artifact_contracts", checks, violations, detail)


# --------------------------------------------------------------------------- #
# KiBot
# --------------------------------------------------------------------------- #
def write_kibot_config(path: str = "kibot.yaml") -> str:
    """Write the template config. It must be edited to match the real stackup."""
    Path(path).write_text(KIBOT_TEMPLATE, encoding="utf-8")
    return path


def run_kibot(board: str, config: str = "kibot.yaml",
              out_dir: str = "production", binary: str = "kibot") -> KiBotRun:
    """Run KiBot. A missing binary is an explicit error, not an empty success."""
    if shutil.which(binary) is None:
        raise FileNotFoundError(
            f"{binary} is not on PATH. The fabrication outputs were NOT "
            "produced; this is not a clean result.")
    proc = subprocess.run([binary, "-b", board, "-c", config, "-d", out_dir],
                          capture_output=True, text=True)
    return KiBotRun(proc.returncode == 0, out_dir, proc.stdout, proc.stderr)


def self_test() -> list[str]:
    """Gate logic only. Does NOT run kicad-cli or kibot."""
    problems: list[str] = []

    # FAULT INJECTION 1: an unrecognised schema must FAIL, not pass.
    unknown = check_report_gate({"something_else": []}, "drc", "b.kicad_pcb", 1)
    if unknown["status"] != FAIL:
        problems.append("an unrecognised DRC schema did not fail closed")

    clean = check_report_gate({"violations": [], "unconnected_items": []},
                              "drc", "b.kicad_pcb", 1)
    if clean["status"] != "PASS":
        problems.append(f"a clean DRC report did not PASS: {clean}")

    # FAULT INJECTION 2: unconnected_items alone must fail the gate. A
    # violations-only reader passed six real error-severity items silently.
    unconnected = check_report_gate(
        {"violations": [],
         "unconnected_items": [{"severity": "error", "type": "unconnected"}]},
        "drc", "b.kicad_pcb", 1)
    if unconnected["status"] != FAIL:
        problems.append("error-severity unconnected_items did not fail the gate")

    # ERC violations live under sheets[]; a top-level reader would miss them.
    erc = check_report_gate(
        {"sheets": [{"path": "/",
                     "violations": [{"severity": "error", "type": "pin"}]}]},
        "erc", "b.kicad_sch", 1)
    if erc["status"] != FAIL:
        problems.append("an ERC error under sheets[] was missed")

    # A warning-only ERC passes.
    warn = check_report_gate(
        {"sheets": [{"path": "/",
                     "violations": [{"severity": "warning", "type": "x"}]}]},
        "erc", "b.kicad_sch", 1)
    if warn["status"] != "PASS":
        problems.append("a warning-only ERC report did not pass")

    if check_contract_gates(None, None)["status"] != NO_COVERAGE:
        problems.append("no contracts supplied did not report NO_COVERAGE")

    # FAULT INJECTION: a design with nothing in it. A clean kicad-cli report
    # on an empty board is "nothing to violate", not "no violations", and
    # scanned was a hard-coded 1 here until 2026-08-10.
    empty = check_report_gate({"violations": [], "unconnected_items": []},
                              "drc", "b.kicad_pcb", 0)
    if empty["status"] != NO_COVERAGE:
        problems.append("a zero-object design reported "
                        f"{empty['status']} instead of NO_COVERAGE")

    # FAULT INJECTION 3: the rollup that decides gate_open. One unmeasured
    # check beside a clean one must NOT open the fabrication gate - this is
    # the exact combination that used to produce gerbers off an ungated board.
    mixed = [result("clean", 1, []),
             Finding("unmeasured", NO_COVERAGE, 0, [], "nothing to examine")]
    if rollup_status(mixed) != NO_COVERAGE:
        problems.append("PASS + NO_COVERAGE rolled up to something other than "
                        "NO_COVERAGE - the fabrication gate would open")
    if rollup_status([]) != NO_COVERAGE:
        problems.append("an empty finding set did not roll up to NO_COVERAGE")
    if rollup_status(mixed, {"unmeasured"}) != PASS:
        problems.append("an explicitly excluded NO_COVERAGE still shut the gate")
    # ...but a measured FAIL cannot be excluded away.
    failed = [Finding("bad", FAIL, 1, [{"problem": "x"}], "measured failure")]
    if rollup_status(failed, {"bad"}) != FAIL:
        problems.append("--exclude suppressed a measured FAIL")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("schematic", nargs="?", help="path to the .kicad_sch")
    parser.add_argument("--out-dir", default="production")
    parser.add_argument("--kibot-config", default="kibot.yaml")
    parser.add_argument("--kibot-bin", default="kibot")
    parser.add_argument("--kicad-cli", help="explicit kicad-cli path")
    parser.add_argument("--parts-contract", help="parts.json to re-read at the gate")
    parser.add_argument("--drc-contract", help="drc.json to re-read at the gate")
    parser.add_argument("--write-drc-contract", metavar="PATH",
                        help="write the DRC result out as a contract file")
    parser.add_argument("--skip-board-checks", action="store_true",
                        help="proceed without pcbnew; the result is NEEDS_HUMAN, "
                             "never PASS. DRC/ERC remain mandatory.")
    parser.add_argument("--skip-verification", action="store_true",
                        help="DEBUGGING ONLY - skips DRC/ERC and everything else")
    parser.add_argument("--no-kibot", action="store_true",
                        help="run the gate only; do not produce outputs")
    parser.add_argument("--exclude", action="append", default=[],
                        metavar="CHECK=REASON",
                        help="put one check deliberately out of scope, e.g. "
                             "--exclude artifact_contracts='no BOM for this "
                             "revision'. The reason is recorded in the report. "
                             "Only an unmeasured (NO_COVERAGE) check can be "
                             "excluded; a measured FAIL shuts the gate anyway. "
                             "Repeatable.")
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
        diag("SELF-TEST PASS: production_outputs")
        return 0

    if not args.board:
        parser.error("the board path is required")
    if not args.schematic:
        parser.error("the schematic path is required (the ERC gate is not optional)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    findings: list[Finding] = []
    if args.skip_verification:
        diag("WARNING: --skip-verification was given; the whole gate was skipped")
        findings.append(Finding(
            "verification_gate", "NO_COVERAGE", 0, [],
            "the verification gate was deliberately skipped; this run proves "
            "nothing about the board"))
    else:
        drc_raw = run_drc(args.board, str(out_dir / "drc.json"), args.kicad_cli)
        erc_raw = run_erc(args.schematic, str(out_dir / "erc.json"), args.kicad_cli)
        findings.append(check_report_gate(
            drc_raw, "drc", args.board, design_census(args.board, "drc")))
        findings.append(check_report_gate(
            erc_raw, "erc", args.schematic,
            design_census(args.schematic, "erc")))
        findings.append(check_board_checks(args.board, args.skip_board_checks))
        findings.append(check_contract_gates(args.parts_contract,
                                             args.drc_contract))
        if args.write_drc_contract:
            write_drc_contract(build_drc_contract(args.board, drc_raw, "drc"),
                               args.write_drc_contract)

    excluded_scopes = []
    for spec in args.exclude:
        check, sep, reason = spec.partition("=")
        if not sep or not reason.strip():
            parser.error(f"--exclude needs CHECK=REASON, got {spec!r}. An "
                         "exclusion without a recorded reason is indistinguish"
                         "able from a gate that was never run.")
        excluded_scopes.append({"check": check.strip(),
                                "reason": reason.strip()})
    excluded = {e["check"] for e in excluded_scopes}

    # The gate used to be ``not any(FAIL)``, which made NO_COVERAGE equivalent
    # to PASS: no contracts supplied, --skip-board-checks or --skip-verification
    # each produced an unmeasured check, and KiBot ran anyway. The schema calls
    # this a "Fail-closed gate in front of the fabrication outputs"; it now is
    # one. Only a PASS rollup opens it, and only an explicitly recorded
    # --exclude can take an unmeasured check out of the rollup.
    gate_verdict = rollup_status(findings, excluded)
    gate_open = gate_verdict == PASS

    kibot_state = None
    if gate_open and not args.no_kibot:
        if not Path(args.kibot_config).exists():
            write_kibot_config(args.kibot_config)
            diag(f"a template KiBot config was written to {args.kibot_config}; "
                 "it must be edited to match the real stackup")
        run = run_kibot(args.board, args.kibot_config, str(out_dir),
                        args.kibot_bin)
        kibot_state = {"ok": run.ok, "out_dir": run.out_dir,
                       "stderr": run.stderr[-2000:]}
        findings.append(result("kibot_outputs", 1,
                               [] if run.ok else [{"problem": "KIBOT_FAILED",
                                                   "stderr": run.stderr[-2000:]}],
                               f"outputs in {run.out_dir}/"))
    elif not gate_open:
        unmeasured = [f["check"] for f in findings
                      if f["status"] == NO_COVERAGE and f["check"] not in excluded]
        diag(f"STOPPED ({gate_verdict}): no fabrication output is produced "
             "before the verification gate is clean.")
        if unmeasured:
            diag("NOT MEASURED: " + ", ".join(unmeasured) +
                 " - an unmeasured check is not a passing one. Supply the "
                 "missing input, or take it out of scope deliberately with "
                 "--exclude CHECK=REASON.")

    report = board_report(TOOL_NAME, args.board, findings, params={
        "schematic": args.schematic,
        "out_dir": str(out_dir),
        "kibot": kibot_state,
        "gate_open": gate_open,
        "gate_verdict": gate_verdict,
        "excluded_scopes": excluded_scopes,
        "skipped_verification": args.skip_verification,
        "skipped_board_checks": args.skip_board_checks,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    # NOT ``summary[FAIL]``: that let NO_COVERAGE leave through exit 0, which
    # is the same silent pass one level down. Exit 0 means the documented
    # thing - the gate is clean - and nothing else.
    return 0 if gate_verdict == PASS else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
