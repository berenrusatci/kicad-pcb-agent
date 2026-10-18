#!/usr/bin/env python3
"""Isolated execution and the fallback ladder for a router that dies mid-run.

PORTED FROM ``pcb-tool-v2/otonom_kurtarma_motoru.py``.

THE PROBLEM IT SOLVES
---------------------
``pcbnew``'s C++ core (through SWIG) and the MCP board backend can segfault
or hang. Run in-process, that takes the whole session down; the archive's
notes record a real ``route_trace`` timeout after which the backend could not
be revived at all. ``run_isolated`` executes a target function in a
short-lived subprocess, so a crash or a hang loses ONE subprocess and returns
a structured failure instead of an exception nobody caught.

WHAT CHANGED IN THE PORT
------------------------
  * THE LADDER NO LONGER HARDCODES ITS RUNGS. The archive imported
    ``topolojik_router_koprusu`` and ``otonom_python_router`` at module
    import time, so this file could not even be LOADED without them - and in
    this repo those live under ``scripts/route/``, owned elsewhere. Rungs are
    now declared in the plan as ``module:function`` and resolved inside the
    subprocess, which is also the only place they can safely be imported.
  * A MISSING RUNG IS A REPORTED FAILURE, NOT A CRASH. An unimportable
    module marks that rung ``unavailable`` and the ladder continues; the
    report says which rungs never ran, so "the ladder was exhausted" and
    "the ladder was never installed" are distinguishable.
  * ``needs_human`` is emitted only when every rung was ATTEMPTED and failed.
    The archive's flag conflated that with rungs that could not be imported.
  * Storage moved out of the Obsidian vault - see ``scripts/memory/store.py``.

CLI:
    recovery.py PLAN [--json REPORT] [--store DIR] [--timeout-s N] [--self-test]

PLAN is a JSON document:
    {
      "goal": "route /USB_D_P J1.1 -> U1.4",
      "timeout_s": 30,
      "rungs": [
        {"name": "direct",   "function": "mypkg.router:try_direct",
         "kwargs": {"net": "/USB_D_P"}},
        {"name": "segmented","function": "mypkg.router:try_segmented",
         "kwargs": {"net": "/USB_D_P", "segment_mm": 5.0}}
      ]
    }

A rung SUCCEEDS when its subprocess exits 0 and prints a JSON object whose
``ok`` field is true.

Exit codes: 0 a rung succeeded, 1 the ladder was exhausted, 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import (  # noqa: E402
    EXIT_FAIL, EXIT_OK, diag, emit, run_cli, write_report,
)
from scripts.memory.store import recovery_dir  # noqa: E402

SCHEMA_VERSION = "recovery-report/v1"
DEFAULT_TIMEOUT_S = 30

TIMEOUT = "timeout"
CRASHED = "crashed"
BAD_OUTPUT = "bad_output"
UNAVAILABLE = "unavailable"

_CHILD = """
import importlib
import json
import sys

request = json.loads(sys.argv[1])
module_name, function_name = request["function"].split(":", 1)
try:
    module = importlib.import_module(module_name)
except ImportError as exc:
    print(json.dumps({"unavailable": True, "error": str(exc)}))
    raise SystemExit(0)
function = getattr(module, function_name, None)
if function is None:
    print(json.dumps({"unavailable": True,
                      "error": function_name + " not found in " + module_name}))
    raise SystemExit(0)
print(json.dumps({"result": function(**request["kwargs"])}))
"""


class RecoveryError(RuntimeError):
    """The recovery plan could not be executed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Isolated:
    """One subprocess result. Never raises into the caller."""
    ok: bool
    result: Any = None
    failure: str = ""
    detail: str = ""
    exit_code: int | None = None
    stderr: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "result": self.result, "failure": self.failure,
                "detail": self.detail, "exit_code": self.exit_code,
                "stderr": self.stderr[-2000:]}


def run_isolated(function: str, kwargs: dict | None = None,
                 timeout_s: int = DEFAULT_TIMEOUT_S,
                 python: str | None = None,
                 extra_path: list[str] | None = None) -> Isolated:
    """Call ``module:function(**kwargs)`` in a short-lived subprocess.

    The return value must be JSON-serialisable - pcbnew objects are not, which
    is why the ladder's rungs return plain counts or dicts.

    Four failure modes, all returned as data:
      timeout      - the child hung (the real pcbnew/MCP symptom);
      crashed      - non-zero exit, including a segfault signal;
      bad_output   - exit 0 but stdout was not a JSON object; NEVER treated
                     as success, because an unexpected print would otherwise
                     read as a clean run;
      unavailable  - the module or function does not exist here.
    """
    kwargs = kwargs or {}
    if ":" not in function:
        return Isolated(False, failure=UNAVAILABLE,
                        detail=f"expected module:function, got {function!r}")
    payload = json.dumps({"function": function, "kwargs": kwargs})
    env = dict(os.environ)
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join(
            [*extra_path] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    try:
        proc = subprocess.run(
            [python or sys.executable, "-c", _CHILD, payload],
            capture_output=True, text=True, timeout=timeout_s, env=env,
            check=False)
    except subprocess.TimeoutExpired:
        return Isolated(False, failure=TIMEOUT,
                        detail=f"no result within {timeout_s}s; the child may "
                               "be hung (the pcbnew/MCP failure this exists for)")
    except OSError as exc:
        return Isolated(False, failure=CRASHED, detail=str(exc))

    if proc.returncode != 0:
        return Isolated(False, failure=CRASHED,
                        detail=f"child exited {proc.returncode}",
                        exit_code=proc.returncode, stderr=proc.stderr)
    try:
        document = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return Isolated(False, failure=BAD_OUTPUT,
                        detail="child exited 0 but stdout was not JSON",
                        exit_code=0, stderr=proc.stdout)
    if not isinstance(document, dict):
        return Isolated(False, failure=BAD_OUTPUT,
                        detail="child JSON root was not an object", exit_code=0)
    if document.get("unavailable"):
        return Isolated(False, failure=UNAVAILABLE,
                        detail=str(document.get("error", "")), exit_code=0)
    return Isolated(True, result=document.get("result"), exit_code=0)


# --------------------------------------------------------------------------- #
@dataclass
class Rung:
    name: str
    function: str
    kwargs: dict = field(default_factory=dict)
    timeout_s: int | None = None

    @staticmethod
    def from_dict(raw: dict, index: int) -> "Rung":
        if not isinstance(raw, dict):
            raise RecoveryError(f"rungs[{index}] must be an object")
        for key in ("name", "function"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise RecoveryError(f"rungs[{index}].{key} must be non-empty text")
        kwargs = raw.get("kwargs", {})
        if not isinstance(kwargs, dict):
            raise RecoveryError(f"rungs[{index}].kwargs must be an object")
        timeout = raw.get("timeout_s")
        if timeout is not None and (isinstance(timeout, bool)
                                    or not isinstance(timeout, int)):
            raise RecoveryError(f"rungs[{index}].timeout_s must be an integer")
        return Rung(raw["name"], raw["function"], kwargs, timeout)


def parse_plan(document: Any) -> tuple[str, int, list[Rung]]:
    if not isinstance(document, dict):
        raise RecoveryError("plan root must be an object")
    rungs = document.get("rungs")
    if not isinstance(rungs, list) or not rungs:
        raise RecoveryError("plan.rungs must be a non-empty array")
    timeout = document.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise RecoveryError("plan.timeout_s must be a positive integer")
    goal = document.get("goal", "")
    if not isinstance(goal, str):
        raise RecoveryError("plan.goal must be text")
    return goal, timeout, [Rung.from_dict(r, i) for i, r in enumerate(rungs)]


def climb(goal: str, rungs: list[Rung], timeout_s: int,
          extra_path: list[str] | None = None) -> dict:
    """Try each rung in order; stop at the first success.

    ``needs_human`` is True only when every rung was actually ATTEMPTED and
    failed. Rungs that could not be imported are counted separately: a ladder
    that was never installed is a configuration problem, not an engineering
    one, and telling a human "this needs your judgement" in that case wastes
    the escalation.
    """
    attempts = []
    attempted = unavailable = 0
    started = utc_now()
    for rung in rungs:
        outcome = run_isolated(rung.function, rung.kwargs,
                               rung.timeout_s or timeout_s,
                               extra_path=extra_path)
        attempts.append({"rung": rung.name, "function": rung.function,
                         **outcome.to_dict()})
        if outcome.failure == UNAVAILABLE:
            unavailable += 1
            continue
        attempted += 1
        if outcome.ok:
            return {
                "schema_version": SCHEMA_VERSION,
                "goal": goal, "status": "RECOVERED",
                "succeeded_rung": rung.name, "result": outcome.result,
                "attempts": attempts, "rungs_total": len(rungs),
                "rungs_attempted": attempted, "rungs_unavailable": unavailable,
                "needs_human": False, "started_utc": started,
                "finished_utc": utc_now(),
            }
    status = "EXHAUSTED" if attempted else "NO_LADDER"
    return {
        "schema_version": SCHEMA_VERSION,
        "goal": goal,
        "status": status,
        "succeeded_rung": None,
        "result": None,
        "attempts": attempts,
        "rungs_total": len(rungs),
        "rungs_attempted": attempted,
        "rungs_unavailable": unavailable,
        "needs_human": status == "EXHAUSTED",
        "note": ("every rung ran and failed; this is no longer a tool crash "
                 "but probably a real layout problem"
                 if status == "EXHAUSTED" else
                 "no rung could even be imported; fix the plan's module paths "
                 "before escalating to a human"),
        "started_utc": started,
        "finished_utc": utc_now(),
    }


# --------------------------------------------------------------------------- #
# Self-test targets - module level so the subprocess can import them, and
# **kwargs-accepting because C builtins like math.sqrt are not.
# --------------------------------------------------------------------------- #
def _probe_add(a: float, b: float) -> float:
    return a + b


def _probe_sleep(seconds: float) -> str:
    import time
    time.sleep(seconds)
    return "woke"


def _probe_crash() -> None:
    raise RuntimeError("deliberate fault injection")


def _probe_noise() -> str:
    print("this is not JSON")
    return "unreachable-through-stdout"


def self_test() -> list[str]:
    problems: list[str] = []
    tools = str(Path(__file__).resolve().parents[2])
    target = "scripts.memory.recovery"

    ok = run_isolated(f"{target}:_probe_add", {"a": 3.0, "b": 4.0},
                      timeout_s=30, extra_path=[tools])
    if not ok.ok or ok.result != 7.0:
        problems.append(f"a normal isolated call failed: {ok.to_dict()}")

    # FAULT INJECTION: a crash must be caught, not propagated.
    crash = run_isolated(f"{target}:_probe_crash", {}, timeout_s=30,
                         extra_path=[tools])
    if crash.ok or crash.failure != CRASHED:
        problems.append(f"a deliberate crash was not caught: {crash.to_dict()}")

    # FAULT INJECTION: a hang must time out without taking us with it.
    hang = run_isolated(f"{target}:_probe_sleep", {"seconds": 5}, timeout_s=1,
                        extra_path=[tools])
    if hang.ok or hang.failure != TIMEOUT:
        problems.append(f"a hang was not timed out: {hang.to_dict()}")

    missing = run_isolated("no_such_module_xyz:go", {}, timeout_s=30)
    if missing.ok or missing.failure != UNAVAILABLE:
        problems.append("a missing module was not reported as unavailable")

    ladder = climb("self-test", [
        Rung("missing", "no_such_module_xyz:go"),
        Rung("crashing", f"{target}:_probe_crash"),
        Rung("working", f"{target}:_probe_add", {"a": 1.0, "b": 1.0}),
    ], 30, extra_path=[tools])
    if ladder["status"] != "RECOVERED" or ladder["succeeded_rung"] != "working":
        problems.append(f"the ladder did not reach a working rung: {ladder}")
    if ladder["rungs_unavailable"] != 1:
        problems.append("the unavailable rung was miscounted")

    exhausted = climb("self-test", [
        Rung("crashing", f"{target}:_probe_crash"),
    ], 30, extra_path=[tools])
    if exhausted["status"] != "EXHAUSTED" or not exhausted["needs_human"]:
        problems.append("an exhausted ladder did not escalate")

    no_ladder = climb("self-test", [Rung("missing", "no_such_module_xyz:go")],
                      30)
    if no_ladder["status"] != "NO_LADDER" or no_ladder["needs_human"]:
        problems.append(
            "a ladder that was never installed escalated to a human anyway")
    return problems


# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", nargs="?",
                        help="recovery plan JSON (see the module docstring)")
    parser.add_argument("--json", dest="json_path", type=Path,
                        help="write the report here as well as to the store")
    parser.add_argument("--store", help="recovery log directory "
                                        "(default: pcb-agent/var/memory)")
    parser.add_argument("--timeout-s", type=int,
                        help="override the plan's per-rung timeout")
    parser.add_argument("--no-store", action="store_true",
                        help="do not archive the report under the store")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_test and not args.plan:
        parser.error("PLAN is required (a recovery plan JSON path)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        emit({"schema_version": SCHEMA_VERSION,
              "self_test": "PASS" if not problems else "FAIL",
              "problems": problems})
        return EXIT_OK if not problems else EXIT_FAIL

    plan_path = Path(args.plan).expanduser().resolve()
    goal, timeout, rungs = parse_plan(
        json.loads(plan_path.read_text(encoding="utf-8")))
    report = climb(goal, rungs, args.timeout_s or timeout)
    report["plan"] = str(plan_path)

    if not args.no_store:
        directory = recovery_dir(args.store, create=True)
        stamp = report["started_utc"].replace(":", "").replace("-", "")
        archived = directory / f"recovery_{stamp}.json"
        write_report(report, archived)
        report["archived"] = str(archived)

    write_report(report, args.json_path)
    emit(report)
    diag(f"recovery {report['status']}: "
         f"{report['rungs_attempted']}/{report['rungs_total']} rung(s) tried, "
         f"{report['rungs_unavailable']} unavailable")
    return EXIT_OK if report["status"] == "RECOVERED" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
