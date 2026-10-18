#!/usr/bin/env python3
"""Generate or verify config/tool_registry.json against the files on disk.

WHY THIS EXISTS
---------------
The previous registry was hand-maintained and drifted: every ``local``
locator still carried an ``aiplanlama/`` prefix from a pre-split repo layout,
so ``context_pack`` reported MISSING_REGISTERED_TOOL for every local tool,
and nine scripts that existed on disk were absent from the registry entirely
(meaning no context pack could authorise them). Hashes are mechanical; a
human should not be maintaining them.

    python3 sync_tool_registry.py --check     # verify, exit 1 on drift
    python3 sync_tool_registry.py --write     # regenerate in place

Exit codes: 0 in sync, 1 drift detected, 2 execution error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.hashing import sha256_file  # noqa: E402
from common.report_io import diag, emit, run_cli  # noqa: E402

SCHEMA_VERSION = "pcb-tool-registry/v1"

TOOLS_DIR = Path(__file__).resolve().parents[1]
REPO = TOOLS_DIR.parent
REGISTRY = REPO / "config" / "tool_registry.json"


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


# Locators are relative to the REPO root (pcb-agent/), because that is what
# callers pass as context_pack's --root.
LOCAL_TOOLS: list[dict] = [
    {
        "name": "context-pack",
        "file": TOOLS_DIR / "scripts" / "context_pack.py",
        "version": "pcb-context-pack/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Write reports only inside project root",
            "--postflight executes declared argv; never expose it over MCP",
        ],
    },
    {
        "name": "gate-receipt",
        "file": TOOLS_DIR / "scripts" / "gate_receipt.py",
        "version": "gate-receipt/v1",
        "modes": ["read", "write"],
        "preconditions": ["Bind every input/tool/config by SHA-256"],
    },
    {
        "name": "research-promotion-gate",
        "file": TOOLS_DIR / "scripts" / "research_promotion_gate.py",
        "version": "pcb-research-promotion-report/v2",
        "modes": ["read", "write"],
        "preconditions": [
            "Never edit SKILL, board, or schematic files",
            "Treat domain allowlist and URL liveness only as screening signals",
            "Require human KABUL before promotion",
        ],
    },
    {
        "name": "crosstalk-thinning",
        "file": TOOLS_DIR / "scripts" / "crosstalk_thinning.py",
        "version": "crosstalk-thinning/v1",
        "modes": ["read"],
        "preconditions": [
            "Screening only: model_class=exploratory_closed_form, signoff=false",
            "Never emit SAFE_TO_THIN without dielectric_height_mm, coupled_length_mm and rise_time_ps",
            "Never thin an impedance-controlled net",
            "FEXT is not modelled; microstrip results carry a CONFIRM",
            "Writes no board or schematic file",
        ],
    },
    {
        "name": "zdiff-solver",
        "file": TOOLS_DIR / "scripts" / "zdiff_solver.py",
        "version": "zdiff-solve-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Screening only: model_class=exploratory_closed_form, signoff=false",
            "Closed-form error is systematic (-29.94%..+2.41% in the published Polar comparison)",
            "Controlled impedance requires a 2D field solver against the real fab stackup",
            "That solver now exists: prefer field-solver, which reports both answers and their delta",
            "Target impedance is a required positional argument",
        ],
    },
    {
        "name": "field-solver",
        "file": TOOLS_DIR / "scripts" / "field_solver.py",
        "version": "field-solver-report/v1",
        "modes": ["read"],
        "preconditions": [
            "model_class=numerical_2d_fdm, signoff=false: nominal er, lossless, no roughness, no measured coupon",
            "Validated against the exact analytic coax; rectangles land on the grid, circles staircase",
            "Grid error is bounded only with --refine",
            "Reports the IPC-2141 answer alongside its own, so the closed-form bias is measured",
            "Geometry is a required positional argument",
        ],
    },
    {
        "name": "monte-carlo",
        "file": TOOLS_DIR / "scripts" / "monte_carlo.py",
        "version": "monte-carlo-report/v1",
        "modes": ["read"],
        "preconditions": [
            "signoff=false; yields carry a Wilson 95% interval and the seed",
            "Draws are INDEPENDENT; a tolerance needs an explicit sigma_divisor",
            "DC bias with no vendor derating curve is NO_COVERAGE",
            "circuit is .op only; absent ngspice is NO_COVERAGE",
            "impedance linearises the field solve and refuses a bad linear fit",
            "Subject is a required positional argument",
        ],
    },
    {
        "name": "electrothermal",
        "file": TOOLS_DIR / "scripts" / "board" / "electrothermal.py",
        "version": "electrothermal-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Closes the R(T) loop current_density and thermal_bridge never closed",
            "Loop gain G = I^2*R20*alpha*Rth; G >= 1 means NO steady state and the tool names no temperature",
            "The refusal comes from the closed-form gain, not from the iteration failing to settle",
            "--thermal-resistance-k-per-w is REQUIRED; the loop gain is proportional to it",
            "DC only, one lumped temperature; a real trace has a gradient and a hotter peak",
            "Reports the uncoupled answer alongside, so the size of the old error is visible",
            "A stable gain says a steady state EXISTS, not that its temperature is acceptable",
        ],
    },
    {
        "name": "coupon-plan",
        "file": TOOLS_DIR / "scripts" / "coupon_plan.py",
        "version": "coupon-plan-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Runs BEFORE gerber output: miss the coupon and the board returns unmeasurable",
            "--tdr-rise-time-ps is REQUIRED and never defaulted; it sets the resolution and therefore the length",
            "A coupon shorter than the TDR can resolve reads a transition, not an impedance, and does not fail loudly",
            "er_eff is solved (--solve) or stated (--er-eff), never guessed; the report says which",
            "Plans the coupon; does NOT lay it out, place it on the panel or emit gerber",
            "A coupon shares the stackup, not the routing: it validates the cross-section, not the production net",
        ],
    },
    {
        "name": "loop-stability",
        "file": TOOLS_DIR / "scripts" / "sim" / "loop_stability.py",
        "version": "loop-stability/v1",
        "modes": ["read"],
        "preconditions": [
            "Models NOTHING; it reads a response an AC simulation or a network analyser produced",
            "Runs without a vendor's compensation note: locating a 0 dB crossing is not proprietary, the thresholds are",
            "Finds EVERY 0 dB crossing; a loop is only as good as its worst one",
            "Detects conditional stability: phase past -180 deg while gain is still up, which a crossover margin cannot see",
            "Unwraps phase and REPORTS the wrap count; a margin from wrapped phase is plausibly wrong, not obviously wrong",
            "No crossing in band is NO_COVERAGE, never a pass and never extrapolated",
            "Acceptance thresholds are inputs; without them the margins are computed but unjudged",
            "Each crossing carries its bracketing samples: a crossover from decade-spaced data is not known to four digits",
            "A headerless CSV is refused - magnitude and phase are both plain numbers and swapping them parses cleanly",
        ],
    },
    {
        "name": "rail-budget",
        "file": TOOLS_DIR / "scripts" / "board" / "rail_budget.py",
        "version": "rail-budget/v1",
        "modes": ["read"],
        "preconditions": [
            "Checks what ERC cannot: a netlist proves EN is CONNECTED, never that the rail on it can turn the regulator on",
            "V_EN(HI) is a datasheet input, never defaulted; an undeclared threshold is NO_COVERAGE",
            "A thin POSITIVE margin is a separate finding from a negative one - it passes at 25 C and fails at the corner",
            "Compares the WRITTEN power-up order against the enable graph; only an inversion is a contradiction",
            "An enable cycle is refused; a topological sort that drops it reports a shorter, plausible order",
            "Rails in one enable tier are not ordered against each other - the graph does not order them",
            "Deterministic noise adds linearly, random adds in quadrature; summing deterministic in quadrature understates it",
            "PSRR is a curve: a figure quoted at another frequency is REFUSED, not interpolated",
            "An absent noise budget still produces the sum, but nothing calls it acceptable",
        ],
    },
    {
        "name": "block-catalog",
        "file": TOOLS_DIR / "scripts" / "block_catalog.py",
        "version": "block-plan/v1",
        "modes": ["read"],
        "preconditions": [
            "The side that READS an architecture decision; an unread decision drifts from the design in silence",
            "Emission and verification come from ONE record, so two documents cannot fall out of sync by hand",
            "The catalogue is a data TABLE, not an if-chain: a caller cannot pick the connector and forget its impedance rule",
            "No parameter is defaulted; a missing required parameter is a refusal that names itself",
            "Emits component ROLES with constraints, never MPNs - sourcing has inputs not visible at this phase",
            "Emits no schematic: generated schematics erase edits made behind the generator",
            "Verification is three-valued; 'unverifiable' never folds into 'present', and no netlist is NO_COVERAGE",
            "A net that EXISTS with a generated name is ABSENT for rule-binding purposes - and its name is unstable",
            "Capacitor counting leans on designator prefixes, a convention; the finding says so rather than reporting a confident zero",
        ],
    },
    {
        "name": "shunt-plan",
        "file": TOOLS_DIR / "scripts" / "shunt_plan.py",
        "version": "shunt-plan/v1",
        "modes": ["read"],
        "preconditions": [
            "Turns an UNKNOWN per-rail current into a bench measurement; it does not estimate the current",
            "Sized from the regulator's RATED output, never the expected draw - sizing from the unknown would be circular",
            "No fixed shunt value: the droop budget and the meter floor define a window, and an empty window is the answer",
            "Plans only; it never edits a schematic, because generated schematics erase edits made behind the generator",
            "--rail NAME=REF:PIN is VERIFIED against the netlist; no netlist is NO_COVERAGE, a mismatch is FAIL",
            "Without --max-uncertainty-pct the achievable accuracy is computed but NOT judged",
            "A shunt reads everything downstream: --rail-intended-load flags on-board loads lumped into the reading",
            "Non-Kelvin sensing carries pad and trace resistance in series; without that term no accuracy is claimed",
            "downstream_of_bypass puts resistance in series with the load's charge reservoir; refused without a stated reason",
        ],
    },
    {
        "name": "correlate",
        "file": TOOLS_DIR / "scripts" / "sim" / "correlate.py",
        "version": "correlation-report/v1",
        "modes": ["read"],
        "preconditions": [
            "The first check whose numbers could show a MODEL is wrong; everything upstream is self-consistent",
            "No measurement is NO_COVERAGE: the model is unfalsified, not confirmed",
            "De-embedding is DECLARED by the caller, never inferred; an undeclared fixture is flagged",
            "No interpolation: only frequencies in BOTH sweeps are compared",
            "Needs a declared band; fewer than 8 shared points refuses rather than averaging noise",
            "A small residual means model and measurement AGREE, not that either is right",
            "An er correction from a delay error is a POINTER; nothing applies it automatically",
        ],
    },
    {
        "name": "priors",
        "file": TOOLS_DIR / "scripts" / "memory" / "priors.py",
        "version": "prior-report/v1",
        "modes": ["read"],
        "preconditions": [
            "model_class=observed_frequency: counts what happened, NOT a trained model",
            "Ranked by the Wilson 95% lower bound so 1/1 cannot outrank 40/40",
            "Below --min-attempts a fix is INSUFFICIENT_EVIDENCE and is not ranked; an empty memory recommends nothing",
            "needs_human is excluded from the denominator: an undecided attempt is not a lost one",
            "Calibration is leave-one-out and is uninformative with fewer than 2 populated bins",
            "Evidence is THIS project's history; it says nothing about a different stackup or fab",
            "Reads the error memory; writes only its own --json",
        ],
    },
    {
        "name": "benchmark",
        "file": TOOLS_DIR / "scripts" / "benchmark.py",
        "version": "benchmark-report/v1",
        "modes": ["read"],
        "preconditions": [
            "scope=tool_invocations: measures TOOLS, not the agent's tool choice; not an end-to-end success rate",
            "--repetitions must be >= 2; one run cannot detect nondeterminism",
            "Pass rate is over COVERED runs; an absent dependency is NO_COVERAGE, not a failure",
            "Determinism is TRI-STATE: null means the tool printed no JSON to compare, not that it drifted",
            "A problem set is a sample: it says what these invocations do, not what every invocation would do",
            "Runs the tools it benchmarks as subprocesses; writes only its own --json",
        ],
    },
    {
        "name": "sparam",
        "file": TOOLS_DIR / "scripts" / "sim" / "sparam.py",
        "version": "sparam-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Requires scikit-rf; its absence is NO_COVERAGE, never a pass",
            "se2gmm's pairing was MEASURED by a three-layout probe plus closed-form skew agreement: ports 2k-1 and 2k form pair k",
            "to_mixed_mode takes the pairing EXPLICITLY; a conductor-numbered s4p fed unpermuted reports a dead channel for a perfect one",
            "Touchstone extension is derived from the port count; skrf picks its format from the file name",
            "cascade() takes 2-ports unless allow_multiport is set; it refuses mismatched frequency axes rather than let skrf interpolate",
            "Carries no channel mask of its own: IL/RL/mode-conversion limits are caller inputs",
            "--line synthesises a uniform TEM trace, not a link: no vias, connectors, launches or bends",
            "--line is LOSSLESS unless --tan-delta is given; the report states which",
        ],
    },
    {
        "name": "parasitics",
        "file": TOOLS_DIR / "scripts" / "sim" / "parasitics.py",
        "version": "parasitics-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Reads routed geometry out of the .kicad_pcb TEXT via common/sexpr.py - no pcbnew, no second process; the JSON/IPC bridge was measured to be unnecessary",
            "--stackup is REQUIRED and every layer must carry a source: an er and a height multiply every number downstream",
            "A routed layer absent from the stackup is SKIPPED and reported, never given default values",
            "An unrouted board is refused: no parasitics is not the same as small parasitics",
            "DC resistance only - no skin effect, so R is optimistic at speed",
            "No coupling BETWEEN segments: each is solved alone over its plane, so crosstalk is absent by construction",
            "Vias are COUNTED and NOT MODELLED; via inductance is missing from every number",
            "Lumped single pi section per net; the report carries the frequency above which that shape is wrong",
            "model_class SCREEN, signoff false: narrows where a layout hurts, never certifies that it does not",
        ],
    },
    {
        "name": "eye",
        "file": TOOLS_DIR / "scripts" / "sim" / "eye.py",
        "version": "eye-report/v1",
        "modes": ['read'],
        "preconditions": [
            "signoff=false: PEAK DISTORTION bound, not a BER contour - a real eye at 1e-12 is "
            "WIDER, so a FAIL here is not proof the link is unusable",
            "No transmitter/receiver model: no FFE/CTLE/DFE, no jitter, no crosstalk, no package",
            "Opening is a FRACTION of full differential swing; volts only with --swing-v, never "
            "a guessed driver level",
            "--ui-ps is required: an eye is defined per bit",
            "Refuses a UI the frequency axis cannot resolve (<4 samples/UI would be a sampling "
            "artefact, not a channel property)",
            "Extrapolates to DC explicitly; skrf only WARNS and returns a wrong step response",
            "Port order is declared, not sniffed - Touchstone does not record it",
        ],
    },
    {
        "name": "stackup-plan",
        "file": TOOLS_DIR / "scripts" / "stackup_plan.py",
        "version": "stackup-plan-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Screening only: model_class=exploratory_closed_form, signoff=false",
            "Design-intent JSON is a required positional argument",
            "Layer roles are rules of thumb; routing feasibility is not proven",
            "UNRESOLVED (no valid layer count) exits 1 and is never a pass",
            "Fab limits come from fixtures/references/fab_profiles.json and are "
            "representative until re-verified against the vendor's page",
            "Writes no board or schematic file",
        ],
    },
    {
        "name": "sync-tool-registry",
        "file": TOOLS_DIR / "scripts" / "sync_tool_registry.py",
        "version": "pcb-tool-registry/v1",
        "modes": ["read", "write"],
        "preconditions": ["Writes only config/tool_registry.json"],
    },
    # --- handoff export: the only tools living in a skill's own directory --- #
    # Locators are repo-relative and this list is explicit rather than a
    # directory scan, so a skill that ships its own executable does not have to
    # scatter itself across tools/scripts/ to be hash-bound.
    #
    # Both halves are registered on purpose. Binding only the shell front end
    # would leave the engine free: someone could change what the export
    # measures while `--check` still reported the registry in sync.
    {
        "name": "gptpro-export",
        "file": REPO / "skills" / "gptpro" / "scripts" / "export.sh",
        "version": "gptpro-handoff/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Writes only inside OUTPUT_DIR; never mutates PROJECT_ROOT",
            "Publishes a bundle set only when the coverage arithmetic closes",
            "GREEN asserts snapshot scope and hashes only - never anything "
            "about the PCB design",
        ],
    },
    {
        "name": "gptpro-export-engine",
        "file": REPO / "skills" / "gptpro" / "scripts" / "gptpro_export.py",
        "version": "gptpro-handoff/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Driven by gptpro-export; the plan file is not a public interface",
            "Hashes both tool files into every manifest and receipt",
        ],
    },
    # --- board checkers: require pcbnew (SWIG), all read-only -------------- #
    {
        "name": "dfm-emc-check",
        "input_schema": 'scripts/board/dfm_emc_check.py:DfmEmcCheckInput',
        "output_schema": 'schemas/board_check_report.schema.json',
        "tests": ['tests/test_board_checks.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "dfm_emc_check.py",
        "version": "board-check-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "scanned=0 reports NO_COVERAGE, never a silent PASS",
            "Reads the board file; never writes it",
        ],
    },
    {
        "name": "mask-dam",
        "input_schema": 'scripts/board/mask_dam.py:MaskDamInput',
        "output_schema": 'schemas/board_check_report.schema.json',
        "tests": ['tests/test_board_checks.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "mask_dam.py",
        "version": "board-check-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Straight PCB_TRACK only; arcs and zone fill are out of scope",
            "Reads the board file; never writes it",
        ],
    },
    {
        "name": "stitch-density",
        "input_schema": 'scripts/board/stitch_density.py:StitchDensityInput',
        "output_schema": 'schemas/board_check_report.schema.json',
        "tests": ['tests/test_board_checks.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "stitch_density.py",
        "version": "board-check-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "er_eff is a user input, not derived from the stackup",
            "Reads the board file; never writes it",
        ],
    },
    {
        "name": "via-stub",
        "file": TOOLS_DIR / "scripts" / "board" / "via_stub.py",
        "version": "board-check-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Screening only; sign-off is 3D EM",
            "Reads the board file; never writes it",
        ],
    },
    # --- schematic toolchain: file backend, MUTATES ------------------------ #
    {
        "name": "sch-wire",
        "file": TOOLS_DIR / "scripts" / "sch" / "sch_wire.py",
        "version": "sch-wire/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Library only; no CLI entrypoint",
            "KiCad must be CLOSED before any write",
            "Connectivity proof is the exported netlist, never the drawn wire",
        ],
    },
    {
        "name": "sch-route",
        "file": TOOLS_DIR / "scripts" / "sch" / "sch_route.py",
        "version": "sch-route/v1",
        "modes": ["read"],
        "preconditions": ["Library only; no CLI entrypoint"],
    },
    {
        "name": "sch-wirefirst",
        "file": TOOLS_DIR / "scripts" / "sch" / "wirefirst.py",
        "version": "sch-wirefirst/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Library only; no CLI entrypoint",
            "Etiket ancak LABEL_REASONS'taki bir gerekceyle konulabilir; "
            "component-to-component baglanti icin gecerli gerekce YOKTUR",
            "Tek sayfali tasarimda 'sheet-to-sheet' gerekcesi reddedilir",
            "Cizilen tel baglanti KANITI DEGILDIR; kanit netlist + wire-audit",
        ],
    },
    {
        "name": "wire-audit",
        "file": TOOLS_DIR / "scripts" / "sch" / "wire_audit.py",
        "version": "wire-audit/v1",
        "modes": ["read"],
        "preconditions": [
            "Sayfayi ve netlisti OKUR, hicbirini yazmaz",
            "Netlist verilmezse B ve G kontrolleri calismaz; kapi ACIK degil YOK",
            "name_stitched_ok VARSAYILAN OLARAK BOS: 'GND zaten adla birlesir' "
            "istisnasi cagiran tarafindan YAZILI verilmelidir",
        ],
    },
    {
        "name": "rewire",
        "input_schema": 'scripts/sch/rewire.py:RewireInput',
        "output_schema": 'scripts/sch/rewire.py:RewireOutput',
        "tests": ['tests/test_sch_write_safety.py'],
        "file": TOOLS_DIR / "scripts" / "sch" / "rewire.py",
        "version": "sch-rewire-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "KiCad must be CLOSED before any write",
            "Proves the netlist after writing and rolls back from .bak on mismatch",
            "Never expose over MCP: it mutates schematics",
        ],
    },
    {
        "name": "wireify",
        "input_schema": 'scripts/sch/wireify.py:WireifyInput',
        "output_schema": 'scripts/sch/wireify.py:WireifyOutput',
        "tests": ['tests/test_sch_write_safety.py'],
        "file": TOOLS_DIR / "scripts" / "sch" / "wireify.py",
        "version": "sch-wireify-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "KiCad must be CLOSED before any write",
            "Netlist must be unchanged; rolls back from .bak otherwise",
            "Never expose over MCP: it mutates schematics",
        ],
    },

    # ===================================================================== #
    # Ported from projectE-2archive/pcb-tool-v2 (see docs/PROVENANCE.md).
    # ===================================================================== #
    {
        "name": "ipc-trace-width",
        "file": TOOLS_DIR / "scripts" / "ipc" / "trace_width.py",
        "version": "ipc-calc-report/v1",
        "modes": ['read'],
        "preconditions": [
            "IPC-2152 screening; not a substitute for the real current-capacity curves",
            "Copper constant is derived (OZ_TO_MM*MM_TO_MIL), not the archive's 1.378 mil",
        ],
    },
    {
        "name": "ipc-clearance",
        "file": TOOLS_DIR / "scripts" / "ipc" / "clearance.py",
        "version": "ipc-calc-report/v1",
        "modes": ['read'],
        "preconditions": [
            "IPC-2221 table interpolation; >50 V needs per-design verification against the standard",
        ],
    },
    {
        "name": "ipc-fab-class",
        "file": TOOLS_DIR / "scripts" / "ipc" / "fab_class.py",
        "version": "ipc-check-report/v1",
        "modes": ['read'],
        "preconditions": [
            "IPC-6012 Class 1/2/3 limits; fab profile overrides these defaults",
        ],
    },
    {
        "name": "ipc-dfa",
        "file": TOOLS_DIR / "scripts" / "ipc" / "dfa.py",
        "version": "ipc-check-report/v1",
        "modes": ['read'],
        "preconditions": [
            "IPC-A-610 assembly clearances; assembler DFA rules override",
        ],
    },
    {
        "name": "ipc-via-current",
        "file": TOOLS_DIR / "scripts" / "ipc" / "via_current.py",
        "version": "ipc-calc-report/v1",
        "modes": ['read'],
        "preconditions": [
            "No fixed A-per-via coefficient; plating thickness must come from the fab profile",
        ],
    },
    {
        "name": "dru-emit",
        "input_schema": 'scripts/dru_emit.py:DruEmitInput',
        "output_schema": 'scripts/dru_emit.py:DruEmitOutput',
        "tests": ['tests/test_dru_emit.py'],
        "file": TOOLS_DIR / "scripts" / "dru_emit.py",
        "version": "dru-emit-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Emits .kicad_dru only; the LAST matching rule wins, so general rules come first",
            "Only track_width/clearance/annular_width are verified; others carry verified_constraint=false",
            "Prove the rule ran (fault-injection) before claiming it was injected",
        ],
    },
    {
        "name": "emc-rules",
        "file": TOOLS_DIR / "scripts" / "emc_rules.py",
        "version": "emc-rules-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Screening only: model_class=exploratory_closed_form, signoff=false",
            "f_knee_edge (0.35/tr) and f_nyquist (rate/2) are different quantities; do not mix",
        ],
    },
    {
        "name": "collision-radar",
        "file": TOOLS_DIR / "scripts" / "board" / "collision_radar.py",
        "version": "collision-radar-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Reads the board file; never writes it",
            "Placement truth is this JSON, never a rendered image",
            "Board outline is a bounding box; rounded outlines can give a false negative",
        ],
    },
    {
        "name": "gerber-dfm",
        "file": TOOLS_DIR / "scripts" / "board" / "gerber_dfm.py",
        "version": "gerber-dfm-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Reads exported RS-274X gerbers: evidence independent of symbolic DRC",
            "Unknown apertures are excluded from the gap maths and listed in params.parse_warnings",
        ],
    },
    {
        "name": "mcad-collision",
        "file": TOOLS_DIR / "scripts" / "board" / "mcad_collision.py",
        "version": "mcad-collision-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires kicad-cli STEP export",
            "Bodies are caller-supplied; STEP B-rep is NOT parsed",
            "Components without body data are reported as untested, never as clean",
        ],
    },
    {
        "name": "thermal-bridge",
        "file": TOOLS_DIR / "scripts" / "board" / "thermal_bridge.py",
        "version": "thermal-bridge-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "KiCad must be CLOSED before any write",
            "Verifies its own output and rolls back from .bak",
            "Never expose over MCP: it mutates the board",
        ],
    },
    {
        "name": "decoupling-coverage",
        "file": TOOLS_DIR / "scripts" / "board" / "decoupling_coverage.py",
        "version": "board-check-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "DISCOVERS IC power pins from the board - complements measure-board, which only "
            "checks the REF.PAD:REF.PAD pairs the caller names",
            "Heuristic: IC detection by footprint package name, power pins by net-name token - "
            "an unrecognised footprint is UNSCANNED, never silently clean; always read 'scanned'",
            "Straight-line pad-centre distance, not the real current loop; necessary but not sufficient",
            "'capacitor_too_far' and 'no_capacitor' are separate violations: different fixes",
        ],
    },
    {
        "name": "measure-board",
        "file": TOOLS_DIR / "scripts" / "board" / "measure_board.py",
        "version": "measure-board-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Reads the board file; never writes it",
            "Vendored L0 evidence tool: lengths are arc length (GetLength), not chord",
        ],
    },
    {
        "name": "skew-timing",
        "file": TOOLS_DIR / "scripts" / "board" / "skew_timing.py",
        "version": "length-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Derived metric only: converts measure_board's pair_skew_mm to ps; takes no "
            "measurement of its own and needs no pcbnew",
            "REQUIRES a propagation input - --solve (2-D field solver) or --er-eff WITH "
            "--er-eff-source. Never defaults it; escape_geom's typical FR4 velocity is "
            "not reachable from here",
            "A pair with no copper on either net is NO_COVERAGE, never the 0.0 mm skew "
            "measure_board reports for it",
            "No default skew budget: without --budget-ps AND --budget-source the metric "
            "is emitted INCONCLUSIVE (recorded, unjudged), which is not a pass",
            "board_sha256 is copied from the parent report's design.sha256; a parent "
            "without one is refused",
        ],
    },
    {
        "name": "pair-coupling",
        "file": TOOLS_DIR / "scripts" / "board" / "pair_coupling.py",
        "version": "pair-coupling-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Reads the board file; never writes it",
            "Screening only: model_class=exploratory_closed_form, signoff=false",
            "COUPLED 0/0 is NO_COVERAGE, never a pass",
        ],
    },
    {
        "name": "drc-gate",
        "file": TOOLS_DIR / "scripts" / "drc_gate.py",
        "version": "drc-gate-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Always passes --exit-code-violations and --severity-all; without them DRC exits 0 on a dirty board",
            "Vendored from tools/krt/drc.sh, which hid tool failure and hardcoded one board's project file",
            "Every report carries provenance.engine_version, MEASURED with `kicad-cli version` at run time, plus the path-free argv_signature",
            "--compare REFUSES (exit 2) two reports whose kicad-cli version or invocation flags differ or are unknown; a 191-vs-222 delta across 10.0.5 and 10.0.4 is not a measurement",
        ],
    },
    {
        "name": "independent-verify",
        "file": TOOLS_DIR / "scripts" / "independent_verify.py",
        "version": "independent-verify-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Second verifier, independent of DRC",
            "A malformed contract is exit 2, never an empty NO_COVERAGE pass",
        ],
    },
    {
        "name": "place-force-directed",
        "file": TOOLS_DIR / "scripts" / "place" / "force_directed.py",
        "version": "placement-result/v1",
        "modes": ['read'],
        "preconditions": [
            "Proposes coordinates only; it does not write the board",
        ],
    },
    {
        "name": "route-grid-astar",
        "file": TOOLS_DIR / "scripts" / "route" / "grid_astar.py",
        "version": "route-result/v1",
        "modes": ['read'],
        "preconditions": [
            "Last-resort router; the ONLY search budget is --max-nodes (there is no time or bbox limit)",
            "A tripped budget is reported as found=false + reason, and NO_COVERAGE/FAIL both exit 1",
            "cell_mm must be smaller than half of (trace width + clearance) or a corridor can be missed",
            "Produces polylines only; it does NOT write the board",
            "No diff-pair and no net-class awareness: pairs must be handled by the caller",
        ],
    },
    {
        "name": "route-topological",
        "file": TOOLS_DIR / "scripts" / "route" / "topological.py",
        "version": "topological-route-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "pcbnew import is lazy and confined to the write path; the CLI never calls write_tracks()",
            "Single-step shove only; not KiCad's PNS router",
            "An unsupplied lower-layer obstacle list is NOT read as an empty layer: no via is proposed",
            "An empty request set exits 1 (route_found=NO_COVERAGE), not 0",
            "No diff-pair and no net-class awareness; high_speed only forbids vias",
        ],
    },
    {
        "name": "production-contracts",
        "file": TOOLS_DIR / "scripts" / "production" / "assembly.py",
        "version": "production-gate-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Fail-closed on an unrecognised contract schema",
        ],
    },
    {
        "name": "production-freerouting",
        "file": TOOLS_DIR / "scripts" / "production" / "freerouting.py",
        "version": "production-gate-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires java + freerouting.jar; NOT exercised in this environment",
            "Diff pairs are NOT carried into DSN: route and LOCK them before calling",
            "Exits 1 on NO_COVERAGE: 'the router did not run' must not read as 'routed'",
            "WRITES the board on a successful SES import (board.Save); run it on a scratch copy only",
            "A True SES import with an unchanged track count is NOT a routed board",
            "Runs no DRC afterwards: a routing result is not a verdict",
            "A missing jar fails closed by default; --allow-jar-download opts into fetching "
            "it from GitHub releases (untested against the live API, no checksum pinned)",
        ],
    },
    {
        "name": "production-outputs",
        "file": TOOLS_DIR / "scripts" / "production" / "outputs.py",
        "version": "production-gate-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Requires KiBot; NOT exercised in this environment",
            "Re-runs DRC/ERC before emitting fab outputs; fail-closed",
        ],
    },
    {
        "name": "promote",
        "file": TOOLS_DIR / "scripts" / "promote.py",
        "version": "promotion-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Emits a gate-receipt/v1 that gate_receipt.py must validate GREEN",
            "--apply requires --atomic; canonical files are replaced with os.replace",
            "Zero contract verifiers is NO_COVERAGE -> RED, never a pass",
        ],
    },
    {
        "name": "pipeline-adapters",
        "file": TOOLS_DIR / "scripts" / "pipeline_adapters.py",
        "version": "pipeline-adapters/v1",
        "modes": ['read'],
        "preconditions": [
            "Library only: each function forwards to an already-registered tool with no report of its own",
            "No hardcoded sequencing - no function calls another (enforced by test_pipeline_adapters.py's AST check)",
            "--self-test is the only CLI surface; import it as a library for real use",
        ],
    },
    {
        "name": "error-memory",
        "file": TOOLS_DIR / "scripts" / "memory" / "error_memory.py",
        "version": "error-memory-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Writes under var/memory/ only; any path inside a .obsidian tree is refused",
            "Returns known-failed fixes as 'do not retry', never as advice",
        ],
    },
    {
        "name": "recovery",
        "file": TOOLS_DIR / "scripts" / "memory" / "recovery.py",
        "version": "recovery-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Writes under var/memory/ only",
            "EXHAUSTED (all rungs failed) is distinct from NO_LADDER (config fault)",
        ],
    },
    {
        # "ngspice" is taken by the system binary entry below; this is the
        # wrapper script, so it needs its own name.
        "name": "ngspice-bridge",
        "file": TOOLS_DIR / "scripts" / "sim" / "ngspice.py",
        "version": "ngspice-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Requires the ngspice binary (verified against ngspice-46)",
            "Behavioural models must declare what they do NOT model (--model-note)",
            "Simulation is not measurement: signoff stays with the bench",
        ],
    },
    {
        "name": "cross-section",
        "file": TOOLS_DIR / "scripts" / "report" / "cross_section.py",
        "version": "cross-section-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Optional tier: svglib/reportlab/Pillow/poppler, imported lazily",
            "visual_guidance_only=true, measurement_admissible=false: never gate on this",
        ],
    },
    {
        "name": "mechanical-dxf",
        "input_schema": 'scripts/board/mechanical_dxf.py:MechanicalDxfInput',
        "output_schema": 'scripts/board/mechanical_dxf.py:MechanicalDxfOutput',
        "tests": ['tests/test_mechanical_dxf.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "mechanical_dxf.py",
        "version": "keepout-zones/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires ezdxf; reads a REAL DXF outline, never a stubbed one",
            "Produces keepout_zones.json against the mekanik-dxf skill's own schema",
        ],
    },
    {
        "name": "current-density",
        "file": TOOLS_DIR / "scripts" / "board" / "current_density.py",
        "version": "board-check-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Resistive-mesh screening model, not a field solver; see its own accuracy-boundary note",
        ],
    },
    {
        "name": "openems-bridge",
        "file": TOOLS_DIR / "scripts" / "sim" / "openems.py",
        "version": "openems-bridge-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew for geometry extraction; skrf is optional for Touchstone evaluation",
            "No FDTD engine here: prepares/evaluates simulation inputs and outputs only",
        ],
    },
    {
        "name": "route-diff-pair",
        "file": TOOLS_DIR / "scripts" / "route" / "diff_pair.py",
        "version": "board-check-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Locks an ALREADY-close pair with a direct track; does not search a route from scratch",
            "Writes the board: run on a scratch copy only",
        ],
    },
    {
        "name": "route-safe-write",
        "file": TOOLS_DIR / "scripts" / "route" / "safe_write.py",
        "version": "board-check-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Rolls back to the pre-write board on a DRC/unconnected-count regression",
        ],
    },
    {
        "name": "routing-table",
        "file": TOOLS_DIR / "scripts" / "report" / "routing_table.py",
        "version": "routing-table-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Every field is extracted from the board; nothing is guessed or hand-filled",
            "XLSX output needs openpyxl; falls back to CSV-only when it is absent",
        ],
    },
    {
        "name": "pmic-allocation",
        "file": TOOLS_DIR / "scripts" / "pmic_allocation.py",
        "version": "pmic-allocation-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Project-specific rail-to-output greedy match; not a BOM/datasheet-level capability check",
        ],
    },
    {
        "name": "capability-inventory",
        "file": TOOLS_DIR / "scripts" / "report" / "capability_inventory.py",
        "version": "capability-inventory-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Static AST analysis; security/dependency columns are heuristic, not a guarantee",
        ],
    },
    {
        "name": "verify-placement",
        "input_schema": 'scripts/board/verify_placement.py:VerifyPlacementInput',
        "tests": ['tests/test_verify_placement.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "verify_placement.py",
        "version": "placement-guard/v1",
        "modes": ['read'],
        "preconditions": [
            "Does NOT require pcbnew - reads the board as text. Deliberate: a "
            "guard that cannot run in the interpreter the package uses is a "
            "guard that never runs",
            "Frame transform is MEASURED from the Edge.Cuts bounding box; with "
            "no board outline the tool returns 2 and does not pass",
            "Courtyard boxes default to 1.0 x 0.6 mm unless --bodies is given; "
            "zone-overlap detection is only as good as those boxes",
        ],
    },
    {
        "name": "route-audit",
        "input_schema": 'scripts/board/route_audit.py:RouteAuditInput',
        "output_schema": 'schemas/route_audit_report.schema.json',
        "tests": ['tests/test_route_audit.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "route_audit.py",
        "version": "route-audit/v1",
        "modes": ['read'],
        "preconditions": [
            "Does NOT require pcbnew - reads the board as text, for the same "
            "reason as verify-placement",
            "Contains NO clearance numbers of its own. The edge ring comes from "
            "board_outline.json, else .kicad_pro; with neither the result is "
            "NO_COVERAGE, and NO_COVERAGE exits 1 because it is not a PASS",
            "Reports AYRISMA when board_outline.json and .kicad_pro declare "
            "different edge rings, and audits against the DECLARATION - the "
            "two numbers produce two different violation counts and both are "
            "correct for their own rule",
            "Cross-checked against KiCad DRC by UUID on cam-w-og05b10 "
            "(2026-08-10): hole_clearance 49/49, hole_to_hole 3/3, "
            "copper_edge_clearance 21/21 identical; the 25 via findings absent "
            "from KiCad `clearance` are all in `shorting_items`",
            "Track-to-track and pad-to-pad copper clearance are OUT OF SCOPE "
            "and declared as such in the run header; do not read a GREEN here "
            "as a clean DRC",
        ],
    },
    {
        "name": "footprint-factory",
        "input_schema": 'scripts/board/footprint_factory.py:FootprintFactoryInput',
        "tests": ['tests/test_footprint_factory.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "footprint_factory.py",
        "version": "footprint-factory/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Contains NO land-pattern numbers of its own. Every dimension comes "
            "from the declaration JSON, where each one carries a `src`. Writing "
            "a number into this file would cut it from its source - the exact "
            "defect the tool exists to fix",
            "Generates from the MANUFACTURER's recommended land pattern, not "
            "from a package outline standard. The two are different documents "
            "and they disagree: measured 2026-08-10, all three of cam-w's "
            "generic library footprints deviated from their vendor drawing",
            "Courtyard is DERIVED from the real pad extent and the body, never "
            "passed in. A hand-passed courtyard can be smaller than the pads "
            "and nothing would object",
            "Two vendors can specify different land patterns for the SAME "
            "package (Microchip SOT-23 c-c 2.80 vs TI DBV 2.60). Entries are "
            "therefore per-PART, not per-package; a shared package entry would "
            "silently print one of them wrong",
            "--check compares generated against on-disk and reports EKSIK / "
            "AYRISMA / BEYANSIZ. It does NOT verify the numbers against the "
            "datasheet - that is a human reading and lives in the `src` fields",
        ],
    },
    {
        "name": "verify-manifest",
        "file": TOOLS_DIR / "scripts" / "board" / "verify_manifest.py",
        "version": "design-manifest-guard/v1",
        "modes": ['read'],
        "input_schema": "schemas/design_manifest.schema.json",
        "output_schema": None,
        "tests": ["tests/test_verify_manifest.py"],
        "preconditions": [
            "Validates the SSOT (design_manifest.json) and, when given them, "
            "checks that derived files and neighbouring declarations have not "
            "DRIFTED from it - a source whose derivative disagrees is not a "
            "single source",
            "Reuses verify_zones' validation engine; a second engine would "
            "drift from the first",
            "Exit 2 (input unreadable) is RED at the caller, never a skip",
        ],
    },
    {
        "name": "sync-skills",
        "file": TOOLS_DIR / "scripts" / "sync_skills.py",
        "version": "skill-registry/v1",
        "modes": ['read', 'write'],
        "input_schema": None,
        "output_schema": None,
        "tests": ["tests/test_skill_contracts.py"],
        "preconditions": [
            "Materializes pcb-agent/skills into .claude/skills so the harness "
            "can load them; .claude/skills is DERIVED and hand edits are "
            "overwritten on the next sync",
            "--check is the gate mode: missing, stale, orphaned or "
            "frontmatter-less skills are RED, never silently skipped",
        ],
    },
    {
        "name": "verify-zones",
        "input_schema": 'schemas/zones.schema.json',
        "tests": ['tests/test_verify_zones.py'],
        "file": TOOLS_DIR / "scripts" / "board" / "verify_zones.py",
        "version": "zones-contract/v1",
        "modes": ['read'],
        "preconditions": [
            "Validates the DECLARATION (board_outline.json zones), not the "
            "board; a zone that is well-formed here can still be violated in "
            "copper - that is verify-placement's job",
            "Reads one contract with two engines (jsonschema if installed, "
            "else a bundled mini interpreter of the SAME .json). Rules are "
            "never transcribed into Python, so schema and validator cannot "
            "drift apart",
            "Three rules JSON Schema cannot express live in the validator: id "
            "uniqueness, escape_layer must appear in exclude_layers, and "
            "allowed_refs_why keys must be a subset of allowed_refs",
            "An empty or absent zones array is a WARNING, not an error - only "
            "cam-w declares zones today, so this contract binds one board",
        ],
    },
    {
        "name": "reference-plane",
        "file": TOOLS_DIR / "scripts" / "board" / "reference_plane.py",
        "version": "board-check-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG); pin to 10.0.x",
            "Checks segment endpoints and midpoint only; a very short mid-segment void can be missed",
        ],
    },
    {
        "name": "testpoint-plan",
        "file": TOOLS_DIR / "scripts" / "testpoint_plan.py",
        "version": "testpoint-plan/v1",
        "modes": ['read'],
        "preconditions": [
            "Validated against skills/dft-testpoints/references/testpoint_plan.schema.json",
        ],
    },
    {
        "name": "jlcpcb-dfm",
        "file": TOOLS_DIR / "scripts" / "production" / "jlcpcb_dfm.py",
        "version": "jlcpcb-dfm-report/v1",
        "modes": ['read'],
        "preconditions": [
            "CONFIRM: request/response shape is a documented GUESS, not verified against a real "
            "JLCPCB account - see the module's own STATUS note before trusting a result",
            "Endpoint and API key come ONLY from JLCPCB_DFM_ENDPOINT/JLCPCB_API_KEY env vars, never hardcoded",
            "--allow-network is required even when both env vars are set (uploads the design to a third party)",
            "Missing config, network error, or an unrecognised response are all NO_COVERAGE, never a fabricated PASS",
        ],
    },
    {
        "name": "jlc-library",
        "file": TOOLS_DIR / "scripts" / "production" / "jlc_library.py",
        "version": "jlc-library-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires the JLC2KiCadLib PyPI package; -dir/-symbol_lib/--skip_existing flags verified "
            "against its published --help output",
            "Cache is a success MARKER keyed by (lcsc_code, symbol_lib, output_dir), not a file copy - "
            "a different destination is always a cache miss",
            "A missing tool or a non-zero exit raises, never returns a fabricated success",
        ],
    },
    {
        "name": "bom-lifecycle",
        "file": TOOLS_DIR / "scripts" / "production" / "bom_lifecycle.py",
        "version": "lifecycle-report/v1",
        "modes": ['read'],
        "preconditions": [
            "Makes NO network call - the skill's own schema assigns the query to the main agent "
            "(provider.queried_from), so supply data is an INPUT (--supply), never fetched here",
            "A line with no supply data is CONFIRM, never a silent Active; unmeasured risk_score is "
            "null, not 0",
            "NRND/EOL/Obsolete is RED with no prototype exception (MASTER_RULEBOOK Bölüm 0)",
            "risk_score is clamped to the schema's maximum of 1.0; the raw stacked sum is preserved "
            "in the reasons rather than discarded",
            "Output validated against skills/bom-lifecycle/references/lifecycle_report.schema.json "
            "by the test suite, in the full / no-supply / stale states",
        ],
    },
    {
        "name": "cad-assets",
        "file": TOOLS_DIR / "scripts" / "production" / "cad_assets.py",
        "version": "cad-assets-report/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Makes NO network call - download_allowed() only JUDGES a URL (https + a narrow host "
            "allowlist); the agent performs the transfer, so this stays deterministic and testable",
            "Pin numbers are read from the S-expression NESTING via common.sexpr, not by matching "
            "a (number \"..\") token anywhere in the file",
            "Multi-unit / De Morgan duplicates are collapsed; the exposed pad is NEVER filtered out "
            "(MASTER_RULEBOOK Faz 1 requires it counted and GND-tied)",
            "register_assets raises PermissionError unless the pin-count gate passed - a gate a "
            "caller can step around is not a gate",
            "lib-table registration is idempotent: a duplicate nickname makes KiCad refuse to open "
            "the project",
            "Writes into the project library dir and lib-tables; NOT MCP-exposed",
        ],
    },
    {
        "name": "ai-feedback-loop",
        "file": TOOLS_DIR / "scripts" / "sim" / "ai_feedback_loop.py",
        "version": "ai-feedback-loop/v1",
        "modes": ['read', 'write'],
        "preconditions": [
            "Requires pcbnew (KiCad 10 SWIG) for apply_trace_geometry; the solver step itself does not",
            "Delegates geometry correction to common.stackup.solve_impedance_geometry - no closed-form duplicated here",
            "Raises RuntimeError/ConvergenceError on failure - never returns a fabricated PASS",
            "Applies track width only; KiCad has no direct net-to-net gap API, so gap correction is NOT applied",
        ],
    },
    {
        "name": "assert-matrix",
        "file": TOOLS_DIR / "scripts" / "assert_matrix.py",
        "version": "assertion-report/v1",
        "modes": ["read"],
        "preconditions": [
            "MEASURES NOTHING - an assertion binds a claim to a LOCATION in a report another tool "
            "produced; re-deriving the quantity here would give the repo two answers to one question",
            "The matrix level decides what a PASS may establish: A/B must carry an executable "
            "predicate (else UNBACKED_CLAIM), C passes only as record_only, D without a measurement "
            "record is NO_COVERAGE",
            "Numeric operators REQUIRE an explicit tolerance - it may be 0, but a boundary policy is "
            "chosen rather than inherited from float arithmetic",
            "'units' without 'units_selector' does not load (UNITS_UNBOUND); an unlabelled number "
            "compared against a labelled threshold is undetectable afterwards",
            "No wildcard selectors: coverage whose size depends on the report passes vacuously on "
            "the empty set",
            "A selector that finds nothing is PATH_ABSENT -> NO_COVERAGE, never PASS and never FAIL",
            "--prove injects each declared falsifier and requires FAIL; still passing is "
            "VACUOUS_ASSERTION, dropping to NO_COVERAGE is FALSIFIER_NO_COVERAGE",
            "Evidence without a declared hash binding can produce a real result but never GREEN "
            "(SCREEN_ONLY) - the call section 5 of the matrix already made",
            "A declared artifact that is not on disk is ARTIFACT_MISSING -> FAIL, never a quiet "
            "fallback to unbound: a typo must not be the cheapest way to silence a staleness check",
            "An UNASSERTED_RULE is NO_COVERAGE (silence), an ORPHAN_ASSERTION is FAIL (enforcement "
            "aimed at a rule that no longer exists passes forever and reads as coverage)",
            "Rule matching is exact on normalised text, never fuzzy - a reworded row must orphan its "
            "assertion rather than silently re-point it at a neighbour",
            "Reads skills/dogrulama-matrisi/SKILL.md; never writes it",
        ],
    },
    {
        "name": "model-registry",
        "file": TOOLS_DIR / "scripts" / "sim" / "model_registry.py",
        "version": "vendor-model-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Never judges whether a model is ACCURATE - that is sim/correlate.py's question and it "
            "needs a measurement. This answers WHICH model, and may it be used HERE",
            "valid_range is REQUIRED with a unit on every axis; a query outside it is "
            "OUT_OF_ENVELOPE -> NO_COVERAGE, never a clamped or extrapolated number",
            "Out-of-envelope is NOT a design failure - the board is not wrong, the model does not "
            "reach; conflating the two sends somebody hunting for a defect that is not there",
            "An axis the query asks about but the model never declared is NO_COVERAGE, not an "
            "implicit pass",
            "authority has NO DEFAULT: only vendor_published may close a sign-off gate; "
            "vendor_derived and synthetic produce real results and are counted separately",
            "provenance requires vendor + document + revision + accessed - a vendor document "
            "without a revision and a date names a FAMILY of documents",
            "A registered file whose bytes moved is STALE_MODEL -> FAIL: a model swapped under an "
            "id keeps the authority of the one it replaced",
            "No registered sha256 is DIGEST_UNVERIFIED -> NO_COVERAGE, never a silent pass",
            "A superseded model is REFUSED, not annotated - it still produces numbers, which is "
            "why a note is not enough",
            "The declared range is cross-checked against the file itself where readable; a "
            "declaration exceeding its own data is ENVELOPE_OVERCLAIMS -> FAIL",
            "Touchstone reading is delegated to common.tline.read_touchstone - a second reader "
            "here would be the #54 failure repeated on purpose",
            "An unverifiable declaration is ENVELOPE_UNCHECKED -> NO_COVERAGE, never PASS",
            "Makes NO network call and ships no models; it records provenance and gates use",
        ],
    },
    {
        "name": "topology-advise",
        "file": TOOLS_DIR / "scripts" / "topology_advise.py",
        "version": "topology-proposal-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Every other solver optimises WITHIN a fixed topology; this proposes CHANGING it - "
            "adding an element, never tuning one",
            "Emits HYPOTHESES, not fixes: no component value is chosen, no schematic is edited, "
            "and nothing here claims the change works",
            "States a DIRECTION, never a magnitude. A catalogue entry whose text contains a unit "
            "does not load (MAGNITUDE_CLAIMED) - this module runs no solver",
            "Triggered only by FAIL/NO_COVERAGE findings other tools already produced; a PASS is "
            "not a shortfall and proposing against one is recommending work for its own sake",
            "Measures nothing itself - a second measurement here would give the repo two answers "
            "to one question (#51 / #54)",
            "Every entry must name a verifying tool (else UNVERIFIABLE_PROPOSAL) and a refuting "
            "observation (else UNFALSIFIABLE_PROPOSAL)",
            "Every entry must state its COST; one listing only benefits is a sales pitch",
            "Every entry must carry at least one condition that rules it out - a transformation "
            "fitting every shortfall fits none (UNCONDITIONAL_TRANSFORM)",
            "Exclusion facts are SUPPLIED via --context, never inferred: whether a reference plane "
            "is split is what reference_plane.py measures",
            "An EXCLUDED proposal is an answer, not a violation - often the most informative line",
            "Parameter changes an existing solver owns are reported as ALREADY_OWNED rather than "
            "skipped; silence reads as 'no tool covers this' and starts a second implementation",
            "Never returns RED on a design - it cannot find a defect; an unaddressable shortfall "
            "is NO_COVERAGE",
        ],
    },
    {
        "name": "boot-observability",
        "file": TOOLS_DIR / "scripts" / "board" / "boot_observability.py",
        "version": "boot-observability-report/v1",
        "modes": ["read"],
        "preconditions": [
            "Answers the question a bring-up bench asks first: the board does not boot, WHICH rail "
            "failed - by crossing rail_budget's enable graph against testpoint_plan's scope",
            "Counts DISTINGUISHABLE FAILURE CLASSES, not rails-with-testpoints: a plan can cover "
            "every rail by count and still leave two failures producing one symptom",
            "Ambiguity lives INSIDE an enable tier; rails in different tiers are already separated "
            "in time because the later tier never starts",
            "Power-good is NEVER a rail measurement - a regulator asserts PG into a rail held down "
            "by a downstream short; PG-only coverage is NO_COVERAGE, not PASS",
            "One real measurement resolves a two-rail tier: the other becomes distinguishable by "
            "elimination. Distinguishable is still not the same as observed, and the run says so",
            "An observation kind has NO DEFAULT; an uncategorised test point is ignored rather "
            "than assumed to prove a rail",
            "A reset is a separate failure kind: released before its rails, it gives a powered "
            "dead board that looks nothing like a missing rail",
            "A reset waiting on an undeclared rail is refused - the sequence it claims to respect "
            "cannot be checked against anything",
            "An enable cycle is refused, never reported against a fabricated order",
            "Does NOT check physical probe access - there is no placement at this phase, and "
            "testpoint_plan.py already draws that line (phase: early_schematic, physical: null)",
        ],
    },
    {
        "name": "connector-current",
        "file": TOOLS_DIR / "scripts" / "connector_current.py",
        "version": "connector-current-report/v1",
        "modes": ["read"],
        "preconditions": [
            "A connector rating is CAPACITY, never DEMAND: capacity is read from "
            "connector_spec.json and demand from rail_tree.json, and the two must be separate "
            "documents or the cross-check is a document checked against itself",
            "connector_spec's own current.rail_cross_check block is NEVER read as demand - "
            "whoever wrote the capacity wrote it. Where it declares a worst case it is compared "
            "against the rail tree and a disagreement is a FAIL",
            "N x I_rated does not load (capacity.policy 'n_times_rated' is refused): the centre "
            "contact runs hottest, sheds current to the outer ones, and the derating curve "
            "depends on ambient AND on how many adjacent contacts carry",
            "Capacity for more than one contact must be READ for that grouping and carry its "
            "source; a stated group rating without a source is GROUP_RATING_UNSOURCED -> "
            "NO_COVERAGE",
            "The room-temperature rating is NEVER substituted for a missing derating curve - it "
            "is the optimistic number, not a conservative stand-in (konnektor SKILL 1 and 3b)",
            "derating_basis 'adjacent_contact_screen' is LOCAL_POLICY, not a datasheet curve: it "
            "can rule a connector out and never signs one off",
            "A derating with no stated ambient is NO_COVERAGE - a curve is a function of "
            "temperature and 'derated' alone does not say derated to what",
            "rail-tree/v1 pins seven fields and forbids others, so it cannot hold provenance for "
            "i_max_a; the map carries it and an unsourced demand is NO_COVERAGE",
            "The rail -> contact-group mapping is an INPUT (--map). Which rail crosses which "
            "contact is a project fact belonging to system-planning/, not to this tool",
            "A rail neither mapped nor excluded with a reason is MAPPING_INCOMPLETE -> "
            "NO_COVERAGE, never a partial pass over the rails that happened to be listed",
            "No margin is defaulted: without --min-margin-a only the SIGN of capacity - demand "
            "is judged",
            "Compares two DOCUMENTS and measures nothing; contact current sharing stays a C->D "
            "electro-thermal measurement and signoff is pinned false",
        ],
    },
    {
        "name": "interconnect-mating",
        "input_schema": '../skills/arayuz-eslesme-kapisi/references/interconnect_chain.schema.json',
        "output_schema": 'schemas/interconnect_mating_report.schema.json',
        "tests": ['tests/test_interconnect_mating.py'],
        "file": TOOLS_DIR / "scripts" / "interconnect_mating.py",
        "version": "interconnect-mating-report/v1",
        "modes": ["read"],
        "preconditions": [
            "The `eligible` universe is derived from interface_contract.json's REQUIRED link set, "
            "never from the declared chain list: with two links declared and a third forgotten, a "
            "declaration-derived denominator reports eligible=2/scanned=2/violations=0 -> GREEN and "
            "the missing link never enters the universe (docs/BULGU-bos-kontrol-temiz-gorunur.md:79-85)",
            "The chain file may NOT state its own denominator: an eligible/coverage/coverage_hint/"
            "edge_count/declared_eligible key is REFUSED (exit 2), not ignored - ignoring it would "
            "leave a reader believing the number was honoured",
            "Contract and chain must be SEPARATE documents; running with one is refused, because "
            "denominator and numerator from one file is a document checked against itself",
            "A contract-required interface with no chain, or a chain with no mating edge, keeps a "
            "one-edge contract minimum in the denominator and violates MISSING_TRANSITION - "
            "deleting the declaration does not shrink the universe",
            "`medium` is NOT nullable in meaning: a null medium is NO_MATED_PART/RED with the link "
            "NAMED, and the edge stays SCANNED. A cable assembly is a third unit and a separate BOM "
            "line (Digi-Key codes housings 390 != assemblies 399 != flat flex cables)",
            "Both edge classes count: `mating` (face <-> neighbour face = physical fit) and "
            "`internal` (a node's two faces, a cable's two ends = netlist continuity / straight vs "
            "reversed). CAM-W measured why: J101 pin 17 = XVCLK, J201 pin 17 = FSYNC",
            "R4 (position count) is authority=screen_only and does NOT open a signature - 20=20 "
            "passes while pin 1 lands on pin 20. On RPi 5 the CSI and DSI ports are physically "
            "IDENTICAL (22-pin, 0.5 mm, 11.5 mm) and the cables are separate SKUs. The signature is "
            "carried by R10 (pin_to_pin mandatory) + R11 (net identity)",
            "A screen_only FAIL still closes the gate (a 30-contact cable does not fit a 22-contact "
            "socket); a record_only FAIL (R6/R8/R16/R19/R20) adds a CONFIRM cap and does not",
            "R12 (POWER_ON_SIGNAL_PIN) is the killer rule: a wrong pin map in MIPI is not 'does not "
            "work', it is 3V3 riding a D-PHY lane = permanent damage. R6 makes a silent open, R12 a "
            "silent kill; they are not the same priority",
            "NO ALIAS TABLE: net names are compared EXACTLY. MIPI_D0_P != CSI_D0_P is a VIOLATION and "
            "the fix is correcting the contract or the board, never normalising "
            "(ecad-varlik-kalifikasyonu:130-133). Same net set in the wrong order is PIN_ORDER_MISMATCH",
            "A vendor contact-face letter is NEVER a key: Molex A/D, Wurth 1/2, market A/B - and "
            "Molex has no 'B'. Only the normalised SAME_SIDE/OPPOSITE_SIDE enum feeds a decision; the "
            "letter lives in provenance as raw data",
            "An unmeasured quantity is None, never 0, and a comparison over a null is NO_COVERAGE, "
            "never PASS: null == null is not equality",
            "`scanned` is the population actually examined, NOT eligible - excluded: deriving it "
            "makes the arithmetic hold by construction, so excluding a phantom edge would silently "
            "shrink the numerator. A phantom exclusion is INVALID_COVERAGE (exit 2)",
            "An exclusion without a reason stays SCANNED, lands in unjustified_exclusions and forces "
            "RED (sistem-arayuz-sozlesmesi:150-158). Edge ids contain ':' so the parser matches the "
            "longest known id, not the first colon",
            "Provenance is a CAP axis, not a finding: unsourced is neither a measured mismatch (RED) "
            "nor 'nobody looked' (NO_COVERAGE) - it caps at CONFIRM. basis derived/vendor_page -> "
            "SCREEN_ONLY, local_policy -> LOCAL_POLICY; none reaches GREEN",
            "gate.verdict uses the CLOSED 15-value vocabulary; the domain answer (CHAIN_COMPLETE / "
            "CHAIN_BROKEN / MEDIUM_UNSPECIFIED / PINMAP_UNVERIFIED / DECLARED_NONMATCHING) lives in "
            "`domain_state` and never leaks into it. Where both appear, gate.verdict binds",
            "Selects no part (bom-lifecycle), connects no net and opens no schematic (sematik-erc), "
            "measures no volume overlap (mekanik-dxf), computes no current/derating/cycle budget "
            "(konnektor), judges no single part's symbol/footprint identity "
            "(ecad-varlik-kalifikasyonu - this is its SIBLING, not its superior)",
            "writes_board=false, signoff=false, assurance_loop=[L1, L2] with NO L3: a skill that "
            "publishes no PHYSICAL_MEASUREMENT cannot close an L3 rule (coverage-gate.md:75-77). "
            "Physical fit is handed to l3-bringup-karakterizasyon via an l3_rule_universe row where "
            "THIS gate owns the limit; naming the L3 skill as owner_skill is SELF_AUTHORED_LIMIT",
            "Reads the canonical interface-contract/v1 AND the board-local pcb-agent/"
            "interface_contract shape, and RECORDS which one it read: refusing one would close the "
            "gate to the repo's own data, staying silent would hide that two shapes exist",
        ],
    },
    {
        "name": "hand-solder-gate",
        "file": TOOLS_DIR / "scripts" / "sch" / "hand_solder_gate.py",
        "version": "kaitek/hand-solder-gate/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Scope is hand-solder / perfboard / prototype schematics ONLY; the tool refuses "
            "boards whose folder name does not look hand-solder unless --force is given. It is "
            "explicitly NOT applicable to the hierarchical, net-label schematics drawn for fab",
            "L1 is not a gate on its own and the file says so: two separate 'FSYNC' labels, each "
            "sitting on its own 2.54 mm stub at opposite ends of the sheet, both pass L1 while no "
            "wire joins them. The verdict lives in L2 - every pin of a net inside ONE "
            "wire-connected component",
            "Connectivity follows KiCad's own rules: a wire end touching another wire's body (T) "
            "connects, two wires crossing (X) without a junction do NOT",
            "Power nets (GND, VCC, ...) are exempt by default and may be joined by power symbols "
            "plus short stubs; --strict removes that exemption and demands wire for rails too",
            "Exit codes DEVIATE from the repo's canonical 0/1/2: this tool returns 0 GREEN, "
            "2 RED, 3 input missing/unreadable. A caller that reads 2 as 'tool could not answer' "
            "will misread a real violation as an environment failure",
        ],
    },
    {
        "name": "i2c-pullup",
        "input_schema": 'scripts/sch/i2c_pullup.py:I2cPullupInput',
        "output_schema": 'scripts/sch/i2c_pullup.py:I2cPullupOutput',
        "tests": ['tests/test_i2c_pullup.py'],
        "file": TOOLS_DIR / "scripts" / "sch" / "i2c_pullup.py",
        "version": "i2c-pullup/v1",
        "modes": ["read"],
        "preconditions": [
            "Ships NO I2C constant: Standard-mode, Fast-mode and Fast-mode Plus differ only in "
            "what the caller declares, and a baked-in 300 ns would assert a spec revision this "
            "file never read",
            "VDD, VOL, IOL, C_bus and tr each arrive as {value, source:{document, revision}} or "
            "they do not arrive; a bare number is a GAP, and a gap is NO_COVERAGE, never a PASS",
            "The ~0.85 in the matrix note is DERIVED from the declared measurement band "
            "(ln((1-f_lo)/(1-f_hi))), so it is attributable to the clause the caller cited",
            "The pull-up network is resolved from the NETLIST by topology - a two-pin part "
            "bridging the segment and the declared rail - never by designator prefix",
            "Reports the PARALLEL equivalent: two boards each carrying a correct 4.7k present "
            "2.35k, and no single-resistor review catches it",
            "A two-pin bridging part whose value will not read (DNP, a diode) is NO_COVERAGE - "
            "dropping it would bias the equivalent high, which is the direction that passes",
            "A part with more than two pins touching both nets is recorded, not modelled: a "
            "slave IC is indistinguishable from a FET level shifter here",
            "An unresolvable segment or rail net is NO_COVERAGE, never 'no pull-ups found'",
            "One C_bus and one VOL/IOL per segment, both declared: the tool does not derive "
            "capacitance from routing and does not choose the weakest driver for you",
            "DC and RC only: no bus timing, no hold time, no arbitration, no clock stretching",
        ],
    },
    {
        "name": "cad-asset-qualification",
        "file": TOOLS_DIR / "scripts" / "production" / "cad_asset_qualification.py",
        "version": "cad-asset-qualification-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Makes NO network call and does NOT extract datasheets - no PDF parser, no OCR, no table "
            "scraper; the authoritative pin/package record is a declared, human/vendor-authored input",
            "Semantic layer ON TOP of cad-assets, not a replacement: check_pin_count, lifecycle_gate "
            "and register_assets are imported and run underneath",
            "Pin/pad/package data is read from the S-expression NESTING via common.sexpr",
            "Pin names are compared EXACTLY (whitespace-stripped only) - no case folding, no ~{} "
            "overbar rewriting, no alias table: a normaliser is where a mismatch goes to die quietly",
            "A dimension or tolerance the source does not state is an EXCLUDED attribute with a "
            "reason, never a default; an excluded package attribute caps the run at CONFIRM",
            "Provenance-incomplete source (document_id/revision/locator/accessed/snapshot_sha256) "
            "can reach at most CONFIRM, never GREEN",
            "Only a manufacturer land pattern permits GREEN; ipc_derived/local_library with a "
            "recorded derivation is LOCAL_POLICY, without one SCREEN_ONLY - neither is a sign-off",
            "Selects no part (bom-lifecycle owns MPN/lifecycle), connects no net (sematik-erc), "
            "judges no manufacturability (dfm); lifecycle is READ ONLY",
            "Exit 0 only on GREEN - CONFIRM/STALE/LOCAL_POLICY/SCREEN_ONLY/NO_COVERAGE all exit 1, "
            "unlike bom-lifecycle, because this is a release gate",
            "--register writes into the project library dir and lib-tables and is refused unless the "
            "verdict is GREEN; NOT MCP-exposed",
        ],
    },
    {
        "name": "first-article",
        "file": TOOLS_DIR / "scripts" / "production" / "first_article.py",
        "version": "first-article-report/v1",
        "modes": ["read", "write"],
        "preconditions": [
            "Consumes a STRUCTURED assembler/AOI/X-ray/MSL record plus a hash-bound evidence "
            "locator; it never opens the locator and never interprets an image",
            "Runs after Faz 4c: the released Gerber/BOM/CPL package must exist and be "
            "hash-identified before the built unit can be compared with it",
            "Same revision + different package bytes is RED (silent substitution); a different "
            "revision is STALE (good evidence about another board) - two different failures",
            "GREEN is an as-built conformance statement, not a reliability qualification",
            "Emits assembly_acceptance_receipt.json as a gate-receipt/v1 FRAGMENT; "
            "tools/scripts/gate_receipt.py is what validates and closes the gate",
            "A hidden-joint package with no X-ray record, or a required inspection universe with "
            "zero records, is NO_COVERAGE/CONFIRM and never GREEN",
            "--declare-out-of-scope records an explicit exclusion with a reason; it forces "
            "NO_COVERAGE and cannot produce GREEN",
        ],
    },
    {
        "name": "physical-measurement",
        "file": TOOLS_DIR / "scripts" / "lab" / "physical_measurement.py",
        "version": "physical-measurement-manifest/v1",
        "modes": ["read"],
        "preconditions": [
            "The repo's FIRST producer of evidence_origin=PHYSICAL_MEASUREMENT; ten skills listed "
            "L3 and all eighteen published TOOL_REPORT",
            "Talks to NO instrument and takes NO measurement: it decides whether a record somebody "
            "else took is admissible evidence for a named rule on a named unit",
            "Invents no threshold. A limit arrives as an input with owner_skill + document_id + "
            "locator, or the row is NO_COVERAGE - never GREEN, never a default",
            "A limit whose authority names this skill is refused (SELF_AUTHORED_LIMIT): a gate that "
            "writes its own threshold measures itself",
            "The accepted first article is a PRECONDITION; an unaccepted unit (or "
            "gate.serial_release false) is RED, not a note",
            "A record whose board_revision/board_hash/serial/lot do not all match the accepted unit "
            "is STALE - no evidence about this board, not weaker evidence",
            "Uncalibrated is not measured: no calibration id/date is NO_COVERAGE. An unwritten setup "
            "cannot be repeated, so it is not evidence either",
            "TDR/VNA evidence needs reference_plane, measurement_zone, port_map, reference_impedance, "
            "fixture and deembed_method or it is INVALID_SPARAM (highspeed-routing SKILL.md 6)",
            "The value is compared as an interval widened by uncertainty + guard_band; a margin "
            "inside that interval is INCONCLUSIVE, never PASS",
            "Pre-compliance is SCREEN_ONLY, is never compared with a formal limit and never closes a "
            "row; accredited compliance without the lab artifact is CONFIRM",
            "Adds no test point and patches no board: RED opens an L2 edge to the rule's declared "
            "owner (writes_board false)",
        ],
    },
]

MCP_SERVERS: list[dict] = [
    {
        "name": "crosstalk-mcp",
        "file": TOOLS_DIR / "servers" / "crosstalk_mcp_server.py",
        "locator": "pcb-crosstalk",
        "version": "1.0.0",
        "modes": ["read"],
        "preconditions": [
            "Transport only; all engineering verdicts come from crosstalk-thinning",
            "Tool surface is analyze_crosstalk_and_thinning; unknown arguments are rejected",
            "stdlib-only stdio JSON-RPC (MCP 2024-11-05); no mcp package required",
        ],
    },
    {
        "name": "board-checks-mcp",
        "file": TOOLS_DIR / "servers" / "board_mcp_server.py",
        "locator": "pcb-board-checks",
        "version": "1.0.0",
        "modes": ["read"],
        "preconditions": [
            "Transport only; all measurements come from the four board checkers",
            "Requires pcbnew; runs in a separate process from pcb-crosstalk",
            "board argument is confined to PCB_AGENT_BOARD_ROOT and must be .kicad_pcb",
            "Exposes read-only checkers only; never rewire/wireify/context-pack --postflight",
        ],
    },
]

STATIC_TOOLS: list[dict] = [
    {
        "name": "web", "kind": "builtin", "locator": "web.run",
        "version": "runtime", "modes": ["read"], "available": True,
        "preconditions": ["Use primary/official sources for technical claims"],
    },
    {
        "name": "exec", "kind": "builtin", "locator": "exec_command",
        "version": "runtime", "modes": ["read", "write"], "available": True,
        "preconditions": ["Respect task mutation_policy"],
    },
    {
        "name": "kicad-cli", "kind": "system", "locator": "/usr/bin/kicad-cli",
        "version": "10.0.5", "modes": ["read", "write"], "available": True,
        "preconditions": [
            "Use --exit-code-violations for ERC/DRC gates",
            "Use --severity-all for sign-off",
        ],
    },
    {
        "name": "ngspice", "kind": "system", "locator": "/usr/bin/ngspice",
        "version": "46", "modes": ["read"], "available": True,
        "preconditions": [
            "Use for power/analog analysis, never as MIPI CSI-2 SI evidence"],
    },
    {
        "name": "kicad-ipc", "kind": "mcp", "locator": "kicad",
        "version": "project-local", "modes": ["read", "write"], "available": True,
        "preconditions": [
            "KiCad IPC API enabled",
            "pcbnew GUI open with target board",
            "Never parallelize IPC calls",
        ],
    },
]


def build_registry(generated_at: str) -> dict:
    tools: list[dict] = []
    tools.extend(STATIC_TOOLS[:2])          # web, exec first (historical order)

    for spec in LOCAL_TOOLS:
        path: Path = spec["file"]
        tools.append({
            "name": spec["name"],
            "kind": "local",
            "locator": _rel(path),
            "version": spec["version"],
            "sha256": sha256_file(path) if path.is_file() else None,
            "modes": spec["modes"],
            "available": path.is_file(),
            # SOZLESME ALANLARI (2026-08-11, Sutun 3). Yoklugu `null` olarak
            # YAZILIR, anahtar atlanmaz: "sozlesmesi yok" bir bilgidir ve
            # makinece gorunur olmalidir. `test_skill_contracts.py` odak
            # kategorilerdeki araclarda bunlarin DOLU ve COZULEBILIR olmasini
            # sart kosar.
            "input_schema": spec.get("input_schema"),
            "output_schema": spec.get("output_schema"),
            "tests": spec.get("tests"),
            "preconditions": spec["preconditions"],
        })

    for spec in MCP_SERVERS:
        path: Path = spec["file"]
        tools.append({
            "name": spec["name"],
            "kind": "mcp",
            "locator": spec["locator"],
            "version": spec["version"],
            "sha256": sha256_file(path) if path.is_file() else None,
            "modes": spec["modes"],
            "available": path.is_file(),
            "preconditions": spec["preconditions"],
        })

    tools.extend(STATIC_TOOLS[2:])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "generator": "tools/scripts/sync_tool_registry.py",
        "tools": tools,
    }


FIXTURE_MANIFEST = (TOOLS_DIR / "fixtures" / "context_pack" / "projecte_ready"
                    / "context_manifest.json")


def repin_fixture() -> list[str]:
    """Re-pin the committed context-pack fixture's artifact hashes.

    The fixture deliberately binds the REAL config/tool_registry.json - that
    is what makes it a genuine registry-binding test rather than a stub. The
    cost is that any script edit changes the registry hash and would leave
    the fixture STALE. Re-pinning here keeps one command as the source of
    truth for both.
    """
    if not FIXTURE_MANIFEST.is_file():
        return []
    data = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    changed: list[str] = []
    for artifact in data.get("artifacts", []):
        target = REPO / artifact["path"]
        if not target.is_file():
            changed.append(f"MISSING_FIXTURE_ARTIFACT:{artifact['path']}")
            continue
        digest = sha256_file(target)
        if artifact.get("sha256") != digest:
            artifact["sha256"] = digest
            changed.append(f"repinned:{artifact['path']}")
    for gate in data.get("gates", []):
        validator = gate.get("validator") or {}
        target = REPO / validator.get("path", "")
        if target.is_file():
            digest = sha256_file(target)
            if validator.get("sha256") != digest:
                validator["sha256"] = digest
                changed.append(f"repinned:{validator['path']}")
    if changed:
        FIXTURE_MANIFEST.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")   # newline: bkz. REGISTRY yazimi
    return changed


def fixture_drift() -> list[str]:
    """Which committed-fixture hashes no longer match disk."""
    if not FIXTURE_MANIFEST.is_file():
        return []
    data = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    for artifact in data.get("artifacts", []):
        target = REPO / artifact["path"]
        if not target.is_file():
            problems.append(f"MISSING_FIXTURE_ARTIFACT:{artifact['path']}")
        elif sha256_file(target) != artifact.get("sha256"):
            problems.append(f"STALE_FIXTURE_ARTIFACT:{artifact['path']}")
    for gate in data.get("gates", []):
        validator = gate.get("validator") or {}
        target = REPO / validator.get("path", "")
        if not target.is_file():
            problems.append(
                f"MISSING_FIXTURE_VALIDATOR:{validator.get('path')}")
        elif sha256_file(target) != validator.get("sha256"):
            problems.append(f"STALE_FIXTURE_VALIDATOR:{validator['path']}")
    return problems


def drift(current: dict, expected: dict) -> list[str]:
    problems: list[str] = []
    cur = {t["name"]: t for t in current.get("tools", [])}
    exp = {t["name"]: t for t in expected["tools"]}
    for name in sorted(set(exp) - set(cur)):
        problems.append(f"MISSING_FROM_REGISTRY:{name}")
    for name in sorted(set(cur) - set(exp)):
        problems.append(f"UNKNOWN_REGISTRY_ENTRY:{name}")
    for name in sorted(set(cur) & set(exp)):
        for field in ("kind", "locator", "version", "sha256", "available"):
            if cur[name].get(field) != exp[name].get(field):
                problems.append(
                    f"{field.upper()}_DRIFT:{name}:"
                    f"{cur[name].get(field)}!={exp[name].get(field)}")
    return problems


def _lf_suffixes() -> set[str]:
    """Suffixes .gitattributes pins to LF, READ from .gitattributes.

    Deliberately not a hand-written list. A second copy of the rule is a
    second thing to forget: the day someone adds `*.csv text eol=lf` the
    guard has to follow without being edited.
    """
    attrs = REPO.parent / ".gitattributes"
    if not attrs.is_file():
        attrs = REPO / ".gitattributes"
    out: set[str] = set()
    if not attrs.is_file():
        return out
    for line in attrs.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "eol=lf" not in line:
            continue
        pattern = line.split()[0]
        if pattern.startswith("*."):
            out.add(pattern[1:])
    return out


LF_SUFFIXES = _lf_suffixes()


def line_ending_problems(expected: dict) -> list[str]:
    """CRLF on disk in a path .gitattributes pins to LF.

    WHY THIS IS A GATE AND NOT A STYLE NOTE: sha256_file() hashes the WORKING
    TREE, not the blob git stores. .gitattributes declares `*.py text eol=lf`,
    so git's copy is LF. If the disk copy is CRLF the two disagree, and the
    registry silently pins the CRLF digest. `--check` then compares CRLF hash
    against CRLF file and reports GREEN -- on that machine only. Any LF
    checkout (Linux, CI, a fresh clone) sees the same content as a different
    hash and the chain reads as tampered.

    Measured 2026-08-10: six tools (shunt-plan, sparam, eye,
    decoupling-coverage, route-grid-astar, verify-placement) carried CRLF
    digests for five days across six commits while --check stayed GREEN on
    Windows. The drift was real the whole time and invisible the whole time.
    A hash chain whose verdict depends on the checkout platform proves
    nothing, so this is RED, not a warning.
    """
    problems: list[str] = []
    seen: set[Path] = set()

    def scan(label: str, path: Path, shown: str) -> None:
        if path in seen or path.suffix not in LF_SUFFIXES or not path.is_file():
            return
        seen.add(path)
        if b"\r\n" in path.read_bytes():
            problems.append(f"CRLF_ON_DISK:{label}:{shown}")

    for tool in expected.get("tools", []):
        if tool.get("kind") == "local" and tool.get("available"):
            scan(tool["name"], REPO / tool["locator"], tool["locator"])

    # Araç dosyalari yeterli DEGIL: registry'nin KENDISI ve context-pack
    # fixture'i da hash'lenen artefaktlardir, ve ikisi de .json'dur. Muhafiz
    # yalniz .py'ye baksaydi 2026-08-10'da olculen tam olay tekrarlardi -
    # sadece bir uzanti otede.
    scan("registry", REGISTRY, _rel(REGISTRY))
    scan("fixture-manifest", FIXTURE_MANIFEST, _rel(FIXTURE_MANIFEST))
    if FIXTURE_MANIFEST.is_file():
        data = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
        for artifact in data.get("artifacts", []):
            scan("fixture-artifact", REPO / artifact["path"], artifact["path"])
        for gate in data.get("gates", []):
            v = gate.get("validator") or {}
            if v.get("path"):
                scan("fixture-validator", REPO / v["path"], v["path"])
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="verify the registry matches disk; exit 1 on drift")
    mode.add_argument("--write", action="store_true",
                      help="regenerate config/tool_registry.json")
    parser.add_argument("--generated-at", default=None,
                        help="date stamp to record (default: keep existing)")
    args = parser.parse_args(argv)

    existing = {}
    if REGISTRY.is_file():
        existing = json.loads(REGISTRY.read_text(encoding="utf-8"))
    stamp = args.generated_at or existing.get("generated_at") or "unset"
    expected = build_registry(stamp)

    if args.write:
        # Refuse BEFORE writing. A CRLF digest that reaches the registry is
        # indistinguishable from a correct one until someone checks out on a
        # different platform, so the only place to stop it is here.
        crlf = line_ending_problems(expected)
        if crlf:
            for note in crlf:
                diag(note)
            diag(f"refusing to write: {len(crlf)} file(s) are CRLF on disk "
                 f"but .gitattributes pins them to LF; the digest would be "
                 f"platform-dependent. Fix with: "
                 f"python -c \"import pathlib,sys; [p.write_bytes("
                 f"p.read_bytes().replace(b'\\r\\n', b'\\n')) "
                 f"for p in map(pathlib.Path, sys.argv[1:])]\" <files>")
            return 1
        REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" ZORUNLU. Python'un metin modu Windows'ta her "\n"i
        # "\r\n" yapar, yani bu satir newline'sız yazildiginda `--write`
        # KOMUTUNUN KENDISI CRLF bir registry uretiyordu. Registry ayni
        # zamanda context-pack fixture'inin hash'ledigi bir artefakttir, yani
        # ureteç kendi zincirini platforma bagimli hale getiriyordu. Muhafiz
        # (line_ending_problems) bunu YAKALAR ama once URETILMEMESI gerekir.
        REGISTRY.write_text(
            json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        diag(f"wrote {REGISTRY} ({len(expected['tools'])} tools)")
        # The registry file itself is a fixture artifact, so re-pin AFTER
        # writing it, and repeat until stable (the first pass changes the
        # registry hash the fixture records).
        for _ in range(3):
            if not repin_fixture():
                break
        for note in fixture_drift():
            diag(f"fixture: {note}")
        return 0

    problems = (drift(existing, expected) + fixture_drift()
                + line_ending_problems(expected))
    emit({
        "schema_version": "tool-registry-check/v1",
        "registry": _rel(REGISTRY),
        "tools_expected": len(expected["tools"]),
        "problems": problems,
        "status": "GREEN" if not problems else "RED",
    })
    if problems:
        diag(f"{len(problems)} registry drift problem(s); "
             f"run: python3 {_rel(Path(__file__))} --write")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version="tool-registry-check/v1"))
