#!/usr/bin/env python3
"""Command Adapter layer: thin, individually-callable functions over the
phases the archive's ``main.py`` hardcoded into one monolithic ``cmd_run``.

WHY THIS EXISTS, AND WHY IT IS NOT ``cmd_run`` AGAIN
-------------------------------------------------------
The archive chained "environment check -> schematic/ERC -> thermal ->
placement -> DRC -> production" into ONE Python function with a FIXED
order, which is exactly the pattern this bundle's skill-based agent
orchestration replaced: sequencing decisions (which phase, in what order,
whether to stop on a failure) belong to the agent/skill, not to a hardcoded
Python call chain.

This module gives back the CONVENIENCE the archive's phases had - one
importable function per phase, with sensible defaults, instead of an agent
having to re-derive the right arguments to five different tools' own
CLIs/functions every time - WITHOUT reintroducing the fixed pipeline. Every
function below is independent: none of them call each other, none of them
decide what runs next. Each simply forwards to an ALREADY-EXISTING,
independently tested tool:

    check_environment   -> common.toolpaths.check_all_tools
    check_schematic_erc -> common.kicad_cli.run_erc / erc_violations
    check_thermal       -> scripts.board.thermal_bridge.check_thermal_contact
    plan_placement      -> scripts.place.force_directed.solve_placement
    check_drc           -> scripts.drc_gate.gate
    produce_outputs     -> scripts.production.outputs's run_drc/run_erc/
                           check_report_gate/check_board_checks
    recommend_via_size  -> scripts.ipc.via_current.recommend_via

The archive's ``cmd_sistem_atama_plani_uret``/``cmd_device_tree_uret``
subcommands are NOT here: they wrap project-specific tools that live under
``system-planning/`` (VC-ID assignment, device-tree generation), OUTSIDE
this generic ``tools/`` tree by the project's own standing architecture
decision - their adapters live in ``system-planning/command_adapters.py``
instead, which is allowed to depend on this bundle but not the reverse.

Library only: no report of its own, no stdout writes. ``--self-test`` is
the only CLI surface, so this stays registrable (every tool in this bundle
is) without pretending to be a report-producing tool it is not.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import Finding, result  # noqa: E402
from common.kicad_cli import erc_violations, run_erc  # noqa: E402
from common.report_io import diag, run_cli  # noqa: E402
from common.toolpaths import ToolStatus, check_all_tools  # noqa: E402

SCHEMA_VERSION = "pipeline-adapters/v1"
TOOL_NAME = "pipeline_adapters"

#: Tool names in common.toolpaths.check_all_tools()'s output that a run
#: cannot proceed without. Kept in sync by hand with check_all_tools' own
#: entries; see its docstring for the full optional/critical breakdown.
CRITICAL_TOOL_NAMES = ("KiCad CLI",)


def check_environment(kicad_cli: str | None = None) -> dict:
    """Critical-vs-optional tool availability, reduced to a caller-friendly
    shape. ``critical_ok=False`` means nothing downstream can run at all.
    """
    statuses: list[ToolStatus] = check_all_tools(kicad_cli)
    critical_ok = all(s.ok for s in statuses if s.name in CRITICAL_TOOL_NAMES)
    missing_optional = [s.name for s in statuses
                        if not s.ok and s.name not in CRITICAL_TOOL_NAMES]
    return {
        "critical_ok": critical_ok,
        "missing_optional": missing_optional,
        "statuses": [{"name": s.name, "ok": s.ok, "detail": s.detail}
                    for s in statuses],
    }


def check_schematic_erc(sch_path: str, kicad_cli: str | None = None) -> Finding:
    """Run ERC on a schematic and reduce it to one Finding.

    Forwards to ``common.kicad_cli.run_erc``/``erc_violations`` - the SAME
    ERC path ``scripts/production/outputs.py`` uses, so a caller gets an
    identical verdict whichever adapter it went through.
    """
    raw = run_erc(sch_path, cli=kicad_cli)
    violations = erc_violations(raw)
    errors = [v for v in violations if v.get("severity") == "error"]
    return result("schematic_erc", 1, errors,
                 f"{len(violations)} total violation(s), "
                 f"{len(errors)} at error severity")


def check_thermal(components: Sequence, zones: Sequence,
                  critical_power_w: float = 0.5) -> Finding:
    """Forwards to ``scripts.board.thermal_bridge.check_thermal_contact``.

    ``components``/``zones`` are that module's own ``HotComponent``/
    ``ContactZone`` dataclasses - this adapter does not redefine or
    reinterpret them.
    """
    from scripts.board.thermal_bridge import check_thermal_contact
    return check_thermal_contact(components, zones, critical_power_w)


def plan_placement(components: Sequence, netlist: Sequence,
                   board_width_mm: float, board_height_mm: float,
                   constraints: Sequence = (), **kwargs):
    """Forwards to ``scripts.place.force_directed.solve_placement``.

    Proposes coordinates only - like the underlying tool, this NEVER writes
    a board. The archive's own equivalent phase carried the same limit
    (see that module's docstring).
    """
    from scripts.place.force_directed import solve_placement
    return solve_placement(components, netlist, board_width_mm,
                           board_height_mm, constraints, **kwargs)


def check_drc(design_path: str, kicad_cli: str | None = None,
             project: str | None = None) -> dict:
    """Forwards to ``scripts.drc_gate.gate`` - DRC or ERC, auto-detected
    from the file extension by that module's own ``detect_mode``."""
    from scripts.drc_gate import gate
    return gate(Path(design_path), kicad_cli=kicad_cli,
               project=Path(project) if project else None)


def produce_outputs(board_path: str, schematic_path: str,
                    out_dir: str = "production",
                    kicad_cli: str | None = None) -> list[Finding]:
    """The verification gate ``scripts/production/outputs.py`` runs before
    handing a board to KiBot - DRC + ERC + board checks, reduced to
    Findings. Does NOT run KiBot itself: that decision (produce
    fabrication output or not) stays with the caller, exactly like every
    other adapter in this module stops at "here is the verdict".
    """
    from scripts.production.outputs import (
        check_board_checks,
        check_report_gate,
        run_drc,
        run_erc,
    )
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    drc_raw = run_drc(board_path, str(work / "drc.json"), kicad_cli)
    erc_raw = run_erc(schematic_path, str(work / "erc.json"), kicad_cli)
    return [
        check_report_gate(drc_raw, "drc", board_path),
        check_report_gate(erc_raw, "erc", schematic_path),
        check_board_checks(board_path, skip=True),
    ]


def recommend_via_size(current_a: float, fab_profile_key: str,
                       delta_t_c: float = 10.0, plating_oz: float = 1.0):
    """Forwards to ``scripts.ipc.via_current.recommend_via``, resolving the
    fab profile by key the same way that module's own CLI does."""
    from common.stackup import fab_profiles
    from scripts.ipc.via_current import recommend_via
    profiles = fab_profiles()
    if fab_profile_key not in profiles:
        raise ValueError(
            f"unknown fab profile {fab_profile_key!r}; "
            f"valid choices are {sorted(profiles)}")
    return recommend_via(current_a, profiles[fab_profile_key], delta_t_c,
                         plating_oz)


def self_test() -> list[str]:
    """Pure wiring checks. Does NOT run kicad-cli, pcbnew or any external
    tool - each underlying function already proves itself in its own
    module's self-test/test suite; this only proves the forwarding is wired
    correctly."""
    problems: list[str] = []

    # check_environment must reduce check_all_tools' real output shape
    # without losing the critical/optional distinction.
    env = check_environment()
    if "critical_ok" not in env or "missing_optional" not in env:
        problems.append("check_environment dropped an expected key")
    if not isinstance(env["statuses"], list) or not env["statuses"]:
        problems.append("check_environment returned no tool statuses at all")

    # check_thermal must reach the real thermal_bridge function, not a stub -
    # proven by a genuine PASS/FAIL/NO_COVERAGE Finding shape coming back.
    finding = check_thermal([], [])
    if finding["status"] != "NO_COVERAGE":
        problems.append(
            "check_thermal with no components did not report NO_COVERAGE")

    # FAULT INJECTION: an unknown fab profile key must be refused, not
    # silently fall back to some default profile.
    try:
        recommend_via_size(1.0, "NOT_A_REAL_FAB_PROFILE")
    except ValueError:
        pass
    else:
        problems.append(
            "recommend_via_size accepted an unknown fab profile key")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (the "
                             "only CLI surface - this module is a library)")
    args = parser.parse_args(argv)

    if not args.self_test:
        parser.error("this module has no report of its own; "
                     "use --self-test, or import it as a library")

    problems = self_test()
    for problem in problems:
        diag(f"SELF-TEST FAIL: {problem}")
    if problems:
        return 1
    diag("SELF-TEST PASS: pipeline_adapters")
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
