#!/usr/bin/env python3
"""ngspice bridge: run the circuit, measure the rails, refuse to invent numbers.

Exports a SPICE netlist with kicad-cli (or takes one directly), runs ngspice in
batch mode, parses the sweep output and turns rail voltage-drop and AC-band
acceptance into PASS/FAIL findings.

backend: cli  (external binaries: ngspice, optionally kicad-cli)

THE ONE RULE: NO INVENTED NUMBERS
---------------------------------
If ngspice is not installed, this tool produces NO simulation result. Every
check reports NO_COVERAGE and the report says the simulation DID NOT RUN. A
made-up number in a simulation report is more dangerous than no report at all,
because it looks like a PASS. The solver's real version string is written into
the report so a reader can see which solver ran, or that none did.

WINDOWS TRAP (measured, not guessed)
------------------------------------
``ngspice.exe`` is the interactive/GUI build and hangs on EVERY invocation,
including ``--version``, even with stdin at /dev/null. Automation must use
``ngspice_con.exe``. The candidate search therefore starts with the console
build, and stdin is always DEVNULL so a wrong pick times out loudly instead of
hanging silently.

MODEL PROVENANCE IS MANDATORY
-----------------------------
``--model-class`` must be stated. When it is ``behavioural``, ``--model-note``
is REQUIRED: a behavioural PASS and a vendor-model PASS are not the same
claim, and "behavioural, but we never said what it fails to represent" is not
an acceptable report.

Usage:
    ngspice.py <netlist.cir> --watch VOUT --rail VOUT=3.3
               --model-class behavioural --model-note "ideal LDO, no PSRR"
               [--analysis op|dc|ac|tran] [--json out.json]
    ngspice.py <schematic.kicad_sch> --from-schematic ...
    ngspice.py --self-test

Exit: 0 = the simulation ran and every check measured and clean, 1 = at least
one FAIL or NO_COVERAGE, 2 = run error. NO_COVERAGE is non-zero because of
THE ONE RULE above: "ngspice is not installed, so nothing was simulated" is
this tool's headline result, and an exit code that hides it behind 0 is the
same invented PASS the rule exists to prevent.

TUR 3 ADDITION — transient analysis and a decoupling-value suggestion:

  * ``TranAnalysis`` (``.tran``) needed NO changes anywhere else: its sweep
    axis is time, and ``parse_output`` already recognised ``"time"`` as a
    sweep header (the archive's own point in adding it — the parse/run chain
    is sweep-var agnostic).
  * ``decoupling_suggestion`` reads a ``check_voltage_drop`` FAIL finding and
    proposes a capacitor value + placement distance for each rail that
    dropped out of tolerance. It does NOT re-simulate or derive a real
    C = I*dt/dV value (that needs real dI/dt and ESR data this tool does not
    have) - it is a labelled rule-of-thumb escalation table, nothing more.
    Its default placement distance comes from ``common.tuning``
    (1.5 mm), matching ``scripts/board/measure_board.py``'s own
    ``--decoupling-max-mm`` default - the single distance this bundle
    actually enforces elsewhere, not a second, independently-drifting number.
"""

from __future__ import annotations

import argparse
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    board_report,
    result,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.tuning import decoupling_limits, drop_pct_tiers  # noqa: E402

SCHEMA_VERSION = "ngspice-report/v1"
TOOL_NAME = "ngspice"

# Console build FIRST - see the module docstring.
NGSPICE_CANDIDATES = ("ngspice_con", "ngspice")


class ModelClass(str, Enum):
    VENDOR_SPICE = "vendor_spice"    # the manufacturer's own .lib/.sub
    BEHAVIOURAL = "behavioural"      # ideal source/resistor equivalent
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Analyses
# --------------------------------------------------------------------------- #
@dataclass
class OpAnalysis:
    """``.op`` - the operating point. The most direct rail/IR-drop check."""

    def spice_line(self) -> str:
        return ".op"


@dataclass
class DcSweep:
    source: str
    start_v: float
    stop_v: float
    step_v: float

    def spice_line(self) -> str:
        return f".dc {self.source} {self.start_v} {self.stop_v} {self.step_v}"


@dataclass
class AcSweep:
    start_hz: float
    stop_hz: float
    points: int = 20
    kind: str = "dec"    # dec | oct | lin

    def spice_line(self) -> str:
        if self.kind not in ("dec", "oct", "lin"):
            raise ValueError(f"invalid AC sweep kind: {self.kind!r}")
        return f".ac {self.kind} {self.points} {self.start_hz} {self.stop_hz}"


@dataclass
class TranAnalysis:
    """``.tran <step_s> <stop_s> [<start_s>]`` - time-domain analysis.

    Unlike ``DcSweep``/``AcSweep``, the sweep axis is TIME. That needed no
    special-casing anywhere else in this module: ``parse_output`` already
    treats ``"time"`` as a recognised sweep header, so the existing
    parse/run chain (``add_analysis``, ``run_ngspice``) works unchanged.
    """

    step_s: float
    stop_s: float
    start_s: float = 0.0

    def spice_line(self) -> str:
        if self.start_s > 0:
            return f".tran {self.step_s} {self.stop_s} {self.start_s}"
        return f".tran {self.step_s} {self.stop_s}"


@dataclass
class SimResult:
    """Parsed ngspice output. ``data[node]`` is a list of (sweep, value)."""

    data: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    rows: int = 0
    raw: str = ""
    version: str = "UNKNOWN"

    def last(self, node: str):
        series = self.data.get(node)
        return series[-1][1] if series else None

    def minimum(self, node: str):
        series = self.data.get(node)
        return min(v for _, v in series) if series else None


# --------------------------------------------------------------------------- #
# Binary discovery
# --------------------------------------------------------------------------- #
def find_ngspice(explicit: str | None = None) -> str | None:
    """Full path to ngspice, or None. An explicit name is tried ALONE."""
    if explicit is not None:
        return shutil.which(explicit)
    for candidate in NGSPICE_CANDIDATES:
        found = shutil.which(candidate)
        if found:
            return found
    return None


def ngspice_version(explicit: str | None = None, timeout_s: int = 20) -> str:
    """The real version string, for the report. ``"ABSENT"`` when not installed.

    stdin is DEVNULL so that accidentally picking the interactive build times
    out and is reported, rather than hanging the run forever.
    """
    path = find_ngspice(explicit)
    if path is None:
        return "ABSENT"
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                              timeout=timeout_s, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"COULD_NOT_RUN ({exc})"
    text = (proc.stdout or "") + (proc.stderr or "")
    for line in text.splitlines():
        stripped = line.strip().lstrip("*").strip()
        if "ngspice" in stripped.lower():
            return stripped
    first = next((s.strip() for s in text.splitlines() if s.strip()), "")
    return first or "UNKNOWN"


# --------------------------------------------------------------------------- #
# Netlist preparation
# --------------------------------------------------------------------------- #
def export_spice_netlist(schematic: str, out: str = "circuit.cir",
                         cli: str | None = None) -> str:
    """``kicad-cli sch export netlist --format spice``.

    Raises rather than producing an empty netlist on failure.
    """
    from common.kicad_cli import KicadCliError, _require_cli
    exe = _require_cli(cli)
    proc = subprocess.run([exe, "sch", "export", "netlist", "--format", "spice",
                           "--output", out, schematic],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise KicadCliError(f"spice netlist export failed: {proc.stderr}")
    if not Path(out).exists():
        raise KicadCliError(f"{out} was not created; kicad-cli said: {proc.stdout}")
    return out


def components_without_model(netlist: str) -> list[str]:
    """Components that will be simulated meaninglessly, because they have no model.

    MEASURED against real KiCad 10 output, which corrected a first draft. A
    symbol with no ``Spice_Model``/``Sim.Device`` field still appears in the
    netlist, written as ``U4 __U4`` - refdes with a ``__``-prefixed model name,
    NOT the classic ``X`` subckt form. The first draft only looked at ``X``
    lines and therefore caught none of the twelve model-less parts on a real
    schematic. Both patterns are checked now:

      1. any component line whose last token starts with ``__`` (KiCad's "no
         model assigned" marker),
      2. an ``X`` subckt instance with no matching ``.subckt`` and no
         ``.include``/``.lib``.
    """
    defined: set[str] = set()
    for line in netlist.splitlines():
        match = re.match(r"^\.subckt\s+(\S+)", line.strip(), re.IGNORECASE)
        if match:
            defined.add(match.group(1).lower())
    external = any(s.strip().lower().startswith((".include", ".lib"))
                   for s in netlist.splitlines())

    missing: list[str] = []
    for line in netlist.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("*", ".")):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        model = parts[-1]
        if model.startswith("__"):
            missing.append(parts[0])
            continue
        if stripped[0].upper() == "X" and len(parts) >= 3:
            if model.lower() not in defined and not external:
                missing.append(parts[0])
    return missing


def back_annotate(netlist: str, parasitics_path: str | None) -> tuple[str, "Finding"]:
    """Splice a post-layout parasitic library into a pre-layout netlist (#64).

    The pairing is by NAME: a net called ``VDD`` in the schematic gets the
    ``PAR_VDD`` subckt if the library carries one. Nothing is guessed - a
    library with no matching subckt annotates nothing and says so, because a
    "post-layout" run that silently annotated zero nets is the most
    expensive kind of false confidence: it looks like the layout was checked.

    Returns ``(netlist, finding)``. Without a library the netlist is returned
    UNCHANGED and the finding is NO_COVERAGE - a pre-layout simulation is a
    perfectly good answer to a different question, and calling it post-layout
    would be the lie.
    """
    check = "post_layout_back_annotation"
    if not parasitics_path:
        return netlist, Finding(
            check, "NO_COVERAGE", 0, [],
            "no parasitic library given: this is a PRE-layout simulation. "
            "The routed board's R/L/C are absent, not zero - run "
            "scripts/sim/parasitics.py on the .kicad_pcb and pass its "
            "--spice output here (#64).")

    path = Path(parasitics_path)
    if not path.is_file():
        return netlist, Finding(
            check, "NO_COVERAGE", 0, [],
            f"parasitic library not found: {path}. Refusing to fall back to "
            "the pre-layout netlist silently - a missing file and a clean "
            "layout must not produce the same report.")

    library = path.read_text(encoding="utf-8")
    available = set(re.findall(r"^\.subckt\s+PAR_(\S+)", library,
                               re.IGNORECASE | re.MULTILINE))
    if not available:
        return netlist, Finding(
            check, "NO_COVERAGE", 0, [],
            f"{path} carries no PAR_* subckt: there is nothing to annotate "
            "with. An empty library is not an unloaded board.")

    body = netlist.rstrip()
    # EXACT ".end", never a prefix match: ".ends" closes a subckt and a
    # prefix test would silently delete every subckt the schematic already
    # had, leaving a netlist that still simulates and is no longer the
    # circuit anybody drew.
    lines = [line for line in body.splitlines()
             if line.strip().lower() != ".end"]
    spliced = "\n".join(lines + ["", "* --- post-layout parasitics (#64) ---",
                                 library.rstrip(), ".end", ""])
    return spliced, result(
        check, len(available), [],
        f"{len(available)} parasitic subckt(s) spliced in from {path.name}: "
        f"{', '.join(sorted(available))}. MODEL CLASS SCREEN - the extractor "
        "carries no skin effect, no inter-segment coupling and no via "
        "inductance, so this narrows where the layout hurts rather than "
        "certifying that it does not.")


def add_analysis(netlist: str, analysis, watched) -> str:
    """Append the analysis command and a ``.control`` block that prints results.

    ``print`` (an ASCII table on one stream) is chosen over ``wrdata`` (binary
    side files) deliberately: one stream is less fragile to parse and is
    testable against a captured real output.
    """
    if not watched:
        raise ValueError("at least one watched node is required: a simulation "
                         "that does not know what it measures is not a report")
    watch = " ".join(f"v({n})" if not n.lower().startswith("v(") else n
                     for n in watched)
    body = netlist.rstrip()
    if body.lower().endswith(".end"):
        body = body[:-len(".end")].rstrip()
    return (f"{body}\n{analysis.spice_line()}\n"
            f".control\nrun\nprint {watch}\n.endc\n.end\n")


# --------------------------------------------------------------------------- #
# Running and parsing
# --------------------------------------------------------------------------- #
_DATA_ROW = re.compile(r"^\s*(\d+)\s+(.+)$")
_NUMBER = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
# `.op` prints scalars, not a table: `v(vout) = 4.545455e+00`.
_SCALAR_ROW = re.compile(
    r"^\s*([a-zA-Z_][\w()\.\#\-\+]*)\s*=\s*"
    r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*$")


def parse_output(text: str) -> SimResult:
    """Parse ngspice batch ``print`` output - BOTH shapes it produces.

    Sweep table (``.dc`` / ``.ac`` / ``.tran``)::

        Index   v-sweep         v(out)
        --------------------------------
        0	0.000000e+00	0.000000e+00

    Column 1 (Index) is dropped, column 2 is the sweep axis (``v-sweep``,
    ``frequency``, ``time``), the rest are the watched nodes.

    Scalar form (``.op``)::

        v(vout) = 4.545455e+00

    An operating point has no sweep axis, so ngspice prints one
    ``name = value`` line per node instead of a table. The archived parser
    only understood the table and therefore returned ZERO rows for every
    ``.op`` run - which its own docstring called the most direct rail check.
    That silently became NO_COVERAGE on a simulation that had in fact run
    correctly. Measured against ngspice-46. The sweep value for these is 0.0.

    No data at all means an EMPTY result, which the acceptance checks turn
    into NO_COVERAGE - never into a PASS.
    """
    parsed = SimResult(raw=text)
    headers: list[str] = []
    has_sweep = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        scalar = _SCALAR_ROW.match(line)
        if scalar and not line.strip().lower().startswith("index"):
            name, value = scalar.group(1), float(scalar.group(2))
            parsed.data.setdefault(name, []).append((0.0, value))
            parsed.rows += 1
            continue
        if line.strip().lower().startswith("index"):
            headers = line.split()[1:]
            has_sweep = bool(headers) and headers[0].lower() in (
                "v-sweep", "frequency", "time", "sweep", "i-sweep", "temp-sweep")
            parsed.data = {h: [] for h in (headers[1:] if has_sweep else headers)}
            continue
        if not headers:
            continue
        match = _DATA_ROW.match(line)
        if not match:
            continue
        numbers = [float(v) for v in _NUMBER.findall(match.group(2))]
        if not numbers:
            continue
        if has_sweep:
            sweep, values = numbers[0], numbers[1:]
        else:
            sweep, values = 0.0, numbers
        for header, value in zip(headers[1:] if has_sweep else headers, values):
            parsed.data.setdefault(header, []).append((sweep, value))
        parsed.rows += 1

    return parsed


def run_ngspice(netlist_path: str, explicit: str | None = None,
                timeout_s: int = 120) -> SimResult | None:
    """Run ngspice in batch mode. Returns None when ngspice is NOT INSTALLED.

    None rather than an exception, and never a fabricated result: the caller
    must turn it into NO_COVERAGE.

    A non-zero exit code alone does not mean failure - ngspice returns one on
    convergence warnings while still printing usable output - so the number of
    parsed rows is what counts.
    """
    path = find_ngspice(explicit)
    if path is None:
        return None
    if not Path(netlist_path).exists():
        raise FileNotFoundError(f"{netlist_path} not found")
    proc = subprocess.run([path, "-b", netlist_path], capture_output=True,
                          text=True, timeout=timeout_s,
                          stdin=subprocess.DEVNULL)
    parsed = parse_output((proc.stdout or "") + "\n" + (proc.stderr or ""))
    parsed.version = ngspice_version(explicit)
    return parsed


# --------------------------------------------------------------------------- #
# Acceptance
# --------------------------------------------------------------------------- #
@dataclass
class RailTarget:
    """What a supply rail must do. ``min_accept_v`` overrides the tolerance."""

    node: str
    nominal_v: float
    tolerance_pct: float = 5.0
    min_accept_v: float | None = None

    @property
    def lower_limit_v(self) -> float:
        if self.min_accept_v is not None:
            return self.min_accept_v
        return self.nominal_v * (1.0 - self.tolerance_pct / 100.0)


def check_voltage_drop(sim: SimResult | None, targets) -> Finding:
    """Worst-case (minimum) rail voltage against the acceptance limit.

    The MINIMUM is used, not the mean or the last value: a rail that dips below
    the limit mid-sweep and recovers has still reset the MCU.

    A rail whose node was never watched counts as a VIOLATION, not as missing
    coverage - otherwise forgetting to watch a node quietly hides its result.
    """
    targets = list(targets)
    if sim is None:
        return result("sim_voltage_drop", 0, [],
                      "ngspice is not installed - the simulation DID NOT RUN "
                      "and no value was invented")
    if sim.rows == 0:
        return result("sim_voltage_drop", 0, [],
                      "ngspice produced no parsable data rows - the netlist or "
                      "the analysis may be wrong")

    violations: list[dict] = []
    scanned = 0
    for target in targets:
        scanned += 1
        key = target.node if target.node in sim.data else f"v({target.node})"
        if key not in sim.data:
            violations.append({
                "node": target.node,
                "reason": "this node is absent from the simulation output "
                          "(it was never added to the watched nodes)",
            })
            continue
        measured = sim.minimum(key)
        if measured is None:
            continue
        if measured < target.lower_limit_v:
            violations.append({
                "node": target.node,
                "nominal_v": target.nominal_v,
                "lower_limit_v": round(target.lower_limit_v, 6),
                "measured_min_v": round(measured, 6),
                "drop_mv": round((target.nominal_v - measured) * 1000.0, 3),
            })
    return result("sim_voltage_drop", scanned, violations,
                  f"ngspice={sim.version}, data_rows={sim.rows}")


def check_ac_band(sim: SimResult | None, node: str, min_gain_db: float,
                  f_low_hz: float, f_high_hz: float) -> Finding:
    """Gain must not fall below ``min_gain_db`` inside the band.

    ngspice's ``print v(out)`` gives linear magnitude; the dB conversion
    happens here. Points at or below zero (a convergence artefact) are SKIPPED
    and counted in the detail - emitting -inf dB and failing on it would be
    misleading.
    """
    if sim is None or sim.rows == 0:
        return result("sim_ac_band", 0, [],
                      "ngspice absent or produced no output - the AC analysis "
                      "DID NOT RUN")
    key = node if node in sim.data else f"v({node})"
    series = sim.data.get(key, [])
    band = [(f, v) for f, v in series if f_low_hz <= f <= f_high_hz]
    usable = [(f, v) for f, v in band if v > 0]
    skipped = len(band) - len(usable)
    if not usable:
        return result("sim_ac_band", 0, [],
                      f"no usable (positive) data point in {f_low_hz}-{f_high_hz} Hz")
    violations = []
    for frequency, value in usable:
        db = 20.0 * math.log10(value)
        if db < min_gain_db:
            violations.append({"frequency_hz": frequency,
                               "gain_db": round(db, 3),
                               "min_gain_db": min_gain_db})
    return result("sim_ac_band", len(usable), violations,
                  f"band={f_low_hz}-{f_high_hz}Hz, skipped_invalid_points={skipped}")


def check_model_coverage(netlist: str) -> Finding:
    """Components with no SPICE model - they simulate to nothing meaningful."""
    lines = [s for s in netlist.splitlines()
             if s.strip() and not s.strip().startswith(("*", "."))]
    missing = [{"refdes": ref, "reason": "no SPICE model assigned"}
               for ref in components_without_model(netlist)]
    return result("sim_model_coverage", len(lines), missing,
                  "every component in the netlist needs a model before its "
                  "contribution to the result means anything")


# --------------------------------------------------------------------------- #
# Decoupling suggestion (interprets a voltage-drop finding, does not simulate)
# --------------------------------------------------------------------------- #
@dataclass
class DecouplingSuggestion:
    """A capacitor value + placement distance suggestion for one rail."""

    node: str
    capacitance_f: list[float]     # e.g. [100e-9] or [100e-9, 10e-6]
    max_distance_mm: float
    rationale: str


# The RULE-OF-THUMB drop tiers moved to fixtures/references/tuning_defaults.json
# (read via common.tuning.drop_pct_tiers). Their caveat has NOT moved and still
# holds: a precise C = I*dt/dV computation needs transient dI/dt and ESR data
# that check_voltage_drop's finding does not carry, so this answers "how much
# extra capacitance for how bad a drop" only roughly - a real PDN/ESR analysis
# must confirm a critical rail.

# This comment used to claim the constant was "one source rather than a
# second, independently-drifting constant". It was the THIRD copy, and it
# disagreed with decoupling_coverage.py's 3.0 mm. The number now comes from
# common/tuning.py, which is what that claim actually requires.


def decoupling_suggestion(
        finding: Finding,
        max_distance_mm: float | None = None,
        ) -> list[DecouplingSuggestion]:
    """Turn a ``check_voltage_drop`` finding's FAIL violations into capacitor
    suggestions. Does NOT re-run the simulation, only interprets its result.

    Returns an empty list when ``finding["status"] != "FAIL"``: on PASS no
    suggestion is needed, and on NO_COVERAGE none can be produced (no
    simulation data exists - inventing a number here would break the same
    "no invented numbers" rule as the rest of this module).
    """
    if finding["status"] != FAIL:
        return []

    if max_distance_mm is None:
        max_distance_mm, _target = decoupling_limits()

    suggestions: list[DecouplingSuggestion] = []
    for violation in finding["violations"]:
        nominal_v = violation.get("nominal_v")
        drop_mv = violation.get("drop_mv")
        node = violation.get("node")
        if nominal_v is None or drop_mv is None or node is None or nominal_v <= 0:
            continue  # this violation is not the expected shape (e.g. "node absent")
        drop_pct = (drop_mv / 1000.0) / nominal_v * 100.0
        values = next(v for threshold, v in drop_pct_tiers() if drop_pct <= threshold)
        value_text = ", ".join(
            f"{v * 1e9:.0f}nF" if v < 1e-6 else f"{v * 1e6:.0f}uF" for v in values)
        suggestions.append(DecouplingSuggestion(
            node=node,
            capacitance_f=list(values),
            max_distance_mm=max_distance_mm,
            rationale=(
                f"{node}: measured drop {drop_mv:.1f}mV ({drop_pct:.1f}% of "
                f"nominal) - out of tolerance (rule-of-thumb). Suggested: "
                f"{value_text}, within {max_distance_mm}mm of the IC power pin "
                "(consistent with scripts/board/measure_board.py's decoupling "
                "distance check).")))
    return suggestions


# --------------------------------------------------------------------------- #
SAMPLE_OUTPUT = """\
Circuit: * ldo test

Doing analysis at TEMP = 27.000000 and TNOM = 27.000000

No. of Data Rows : 3
Index   v-sweep         v(vout)         v(vin)
------------------------------------------------
0\t0.000000e+00\t3.300000e+00\t4.200000e+00
1\t5.000000e-01\t3.290000e+00\t4.100000e+00
2\t1.000000e+00\t3.100000e+00\t4.000000e+00
"""


def self_test() -> list[str]:
    """Parsing and acceptance only. Does NOT run the real binary."""
    problems: list[str] = []
    sim = parse_output(SAMPLE_OUTPUT)

    if sim.rows != 3:
        problems.append(f"expected 3 data rows, parsed {sim.rows}")
    if sim.minimum("v(vout)") != 3.1:
        problems.append("minimum of v(vout) is not 3.1")

    clean = check_voltage_drop(sim, [RailTarget("vout", 3.3, tolerance_pct=10.0)])
    if clean["status"] != "PASS":
        problems.append(f"expected PASS at 10 % tolerance, got {clean['status']}")

    # FAULT INJECTION: pull the limit above the measured minimum -> must FAIL.
    injected = check_voltage_drop(
        sim, [RailTarget("vout", 3.3, min_accept_v=3.25)])
    if injected["status"] != FAIL:
        problems.append("fault injection did not hold: nothing was compared")

    absent = check_voltage_drop(None, [RailTarget("vout", 3.3)])
    if absent["status"] != "NO_COVERAGE":
        problems.append("a missing ngspice did not report NO_COVERAGE")

    # An unwatched node is a violation, not silent coverage loss.
    unwatched = check_voltage_drop(sim, [RailTarget("v3v3", 3.3)])
    if unwatched["status"] != FAIL:
        problems.append("an unwatched rail node did not FAIL")

    # `.op` scalar form (measured against ngspice-46), which the archived
    # parser could not read at all.
    op = parse_output("Circuit: * t\n\nNo. of Data Rows : 1\n"
                      "v(vout) = 4.545455e+00\nv(vin) = 5.000000e+00\n")
    if op.rows != 2 or op.minimum("v(vout)") != 4.545455:
        problems.append(f"the .op scalar output was not parsed: {op.data}")
    op_check = check_voltage_drop(op, [RailTarget("vout", 5.0, tolerance_pct=1.0)])
    if op_check["status"] != FAIL:
        problems.append("an .op result was not compared against the rail limit")

    if components_without_model("U4 __U4\nY1 __Y1\n") != ["U4", "Y1"]:
        problems.append("KiCad's '__' no-model marker was not detected")

    try:
        add_analysis("* c\n.end\n", OpAnalysis(), [])
    except ValueError:
        pass
    else:
        problems.append("an analysis with no watched node was accepted")

    return problems


def _parse_rail(text: str) -> RailTarget:
    """``NODE=NOMINAL[:TOLPCT]`` or ``NODE=NOMINAL@MINV``."""
    if "=" not in text:
        raise ValueError(f"--rail needs NODE=NOMINAL, got {text!r}")
    node, rest = text.split("=", 1)
    if "@" in rest:
        nominal, minimum = rest.split("@", 1)
        return RailTarget(node, float(nominal), min_accept_v=float(minimum))
    if ":" in rest:
        nominal, tolerance = rest.split(":", 1)
        return RailTarget(node, float(nominal), tolerance_pct=float(tolerance))
    return RailTarget(node, float(rest))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist", nargs="?",
                        help="SPICE netlist (.cir), or a .kicad_sch with "
                             "--from-schematic")
    parser.add_argument("--from-schematic", action="store_true",
                        help="export the SPICE netlist from the schematic first")
    parser.add_argument("--watch", action="append", default=[],
                        help="node to print; repeatable")
    parser.add_argument("--rail", action="append", default=[],
                        help="NODE=NOMINAL[:TOL_PCT] or NODE=NOMINAL@MIN_V; "
                             "repeatable")
    parser.add_argument("--analysis", choices=("op", "dc", "ac", "tran"),
                        default="op")
    parser.add_argument("--dc", metavar="SRC,START,STOP,STEP")
    parser.add_argument("--ac", metavar="FLOW,FHIGH[,POINTS[,KIND]]")
    parser.add_argument("--tran", metavar="STEP_S,STOP_S[,START_S]")
    parser.add_argument("--ac-band", metavar="NODE,MIN_DB,FLOW,FHIGH")
    parser.add_argument("--model-class",
                        choices=[m.value for m in ModelClass],
                        default=ModelClass.UNKNOWN.value)
    parser.add_argument("--model-note", default="",
                        help="what a behavioural model does NOT represent; "
                             "REQUIRED when --model-class is behavioural")
    parser.add_argument("--ngspice", help="explicit ngspice executable")
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
        diag("SELF-TEST PASS: ngspice")
        return 0

    if not args.netlist:
        parser.error("the netlist (or schematic, with --from-schematic) is required")
    if not args.watch:
        parser.error("--watch is required: a simulation that does not state "
                     "what it measures is not a report")
    if (args.model_class == ModelClass.BEHAVIOURAL.value
            and not args.model_note.strip()):
        parser.error("--model-note is required with --model-class behavioural: "
                     "state what the model does not represent")

    if args.analysis == "dc":
        if not args.dc:
            parser.error("--analysis dc requires --dc SRC,START,STOP,STEP")
        source, start, stop, step = args.dc.split(",")
        analysis = DcSweep(source, float(start), float(stop), float(step))
    elif args.analysis == "ac":
        if not args.ac:
            parser.error("--analysis ac requires --ac FLOW,FHIGH[,POINTS[,KIND]]")
        fields = args.ac.split(",")
        analysis = AcSweep(float(fields[0]), float(fields[1]),
                           int(fields[2]) if len(fields) > 2 else 20,
                           fields[3] if len(fields) > 3 else "dec")
    elif args.analysis == "tran":
        if not args.tran:
            parser.error("--analysis tran requires --tran STEP_S,STOP_S[,START_S]")
        fields = args.tran.split(",")
        analysis = TranAnalysis(float(fields[0]), float(fields[1]),
                                float(fields[2]) if len(fields) > 2 else 0.0)
    else:
        analysis = OpAnalysis()

    source_path = args.netlist
    if args.from_schematic:
        source_path = export_spice_netlist(
            args.netlist, str(Path(args.netlist).with_suffix(".cir")))

    netlist_text = Path(source_path).read_text(encoding="utf-8")
    prepared = add_analysis(netlist_text, analysis, args.watch)
    prepared_path = str(Path(source_path).with_suffix(".prepared.cir"))
    Path(prepared_path).write_text(prepared, encoding="utf-8")

    sim = run_ngspice(prepared_path, args.ngspice, args.timeout_s)
    if sim is None:
        diag("ngspice is not installed: the simulation did NOT run and no "
             "value was invented")

    findings = [
        check_model_coverage(netlist_text),
        check_voltage_drop(sim, [_parse_rail(r) for r in args.rail]),
    ]
    if args.ac_band:
        node, min_db, f_low, f_high = args.ac_band.split(",")
        findings.append(check_ac_band(sim, node, float(min_db),
                                      float(f_low), float(f_high)))

    report = board_report(TOOL_NAME, source_path, findings, params={
        "analysis": analysis.spice_line(),
        "watched_nodes": args.watch,
        "prepared_netlist": prepared_path,
        "model_class": args.model_class,
        "model_note": args.model_note,
        "solver": sim.version if sim else "ABSENT",
        "simulation_ran": sim is not None and sim.rows > 0,
        "data_rows": sim.rows if sim else 0,
    }, schema_version=SCHEMA_VERSION)
    report["netlist"] = report.pop("board")
    write_report(report, args.json)
    emit(report)
    # This branched on FAIL alone - a hand-rolled copy of
    # common.findings.board_exit_code that never inherited that function's
    # documented (and deliberately frozen) NO_COVERAGE exemption. It was the
    # worst place in the bundle for it: an absent ngspice, a run with no
    # parsable data rows and an AC band with no usable point all report
    # NO_COVERAGE, so exit 0 turned "the simulation DID NOT RUN" into the
    # same signal as "the rails held" - the invented PASS THE ONE RULE
    # forbids, arriving through the exit code instead of through a number.
    summary = report["summary"]
    return 1 if summary[FAIL] or summary[NO_COVERAGE] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
