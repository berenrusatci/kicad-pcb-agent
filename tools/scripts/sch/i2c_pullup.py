#!/usr/bin/env python3
"""Check every I²C segment's pull-up network against its own [Rmin, Rmax].

WHY A SCRIPT AND NOT A FORMULA IN A DOCUMENT
--------------------------------------------------------------------------
The verification matrix has carried

    Rmin = (VDD - VOL) / IOL        Rmax ~= tr / (0.85 * C_bus)

as prose for as long as the matrix has existed, and prose has never once
divided anything. The row stayed at level C for the reason every C row
stays at C: the formula was written down, the design was not measured
against it. What ERC cannot see here is not the arithmetic - it is that
TWO boards, each carrying a correct 4.7k pull-up, joined over a connector,
present 2.35k to a driver specified at 3 mA. Both schematics are right.
The bus is not. Nothing in a netlist objects, because nothing in a netlist
knows what a pull-up is for.

So this tool resolves the network from the netlist by TOPOLOGY - a
two-terminal part with one pin on the segment and one on the declared rail -
takes the parallel equivalent, and compares it against a window built from
inputs the caller had to cite.

WHAT MAKES THIS NO_COVERAGE RATHER THAN PASS
--------------------------------------------------------------------------
Five numbers set the window and not one of them is in the design: VDD, VOL,
IOL, C_bus and the selected mode's tr. Every one arrives as
``{"value": ..., "source": {"document": ..., "revision": ...}}`` or it does
not arrive. A bare number is a gap. A source without a revision is a gap. A
gap is NO_COVERAGE, never a PASS carrying a plausible default, because the
plausible default here is somebody else's board.

The tool ships NO I²C constant. Standard-mode, Fast-mode and Fast-mode Plus
differ only in what the caller declares; a "300 ns" baked in here would be
this file asserting a spec revision it never read.

Usage:
    i2c_pullup.py --netlist build/netlist.net --segments i2c_segments.json
                  [--json i2c_pullup.json]
    i2c_pullup.py --self-test

``i2c_segments.json`` (``i2c-segment-input/v1``):
    {"segments": [
      {"bus": "SENSOR_I2C",
       "mode": "Fast-mode",
       "rail_net": "+3V3",
       "lines": {"SDA": "SENSOR_SDA", "SCL": "SENSOR_SCL"},
       "inputs": {
         "vdd_v":    {"value": 3.3,   "source": {...}},
         "vol_v":    {"value": 0.4,   "source": {...}},
         "iol_a":    {"value": 0.003, "source": {...}},
         "c_bus_f":  {"value": 1e-10, "method": "estimated", "source": {...}},
         "t_rise_s": {"value": 3e-7,  "source": {...}},
         "rise_time_band": {"low_fraction": 0.3, "high_fraction": 0.7,
                            "source": {...}}}}]}

Every ``source`` is ``{"document": ..., "revision": ..., "clause": ...}``;
``clause`` is optional, the other two are not. ``rise_time_coefficient``
(itself sourced) may replace ``rise_time_band`` when the source states the
multiplier directly - the report records which of the two was used.

Exit codes: 0 = GREEN, 1 = RED or NO_COVERAGE, 2 = bad input or run error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, NO_COVERAGE, result, summarise  # noqa: E402
from common.hashing import sha256_file  # noqa: E402
from common.i2c import (IN_WINDOW, MISSING_INPUT, MISSING_MODE,  # noqa: E402
                        NO_PULLUP, VERDICT_DETAIL, Gap, classify,
                        parallel_ohms, parse_resistance, r_max_ohms,
                        r_min_ohms, read_rise_time_band, read_sourced,
                        resolve_pullups, rise_time_coefficient)
from common.netlist import (parse_nets, read_components,  # noqa: E402
                            read_nets)
from common.report_io import (diag, emit, run_cli, status_exit_code,  # noqa: E402
                              write_report)


# --------------------------------------------------------------------------- #
# AI sözleşmesi: aracın girdi/çıktı yüzeyi. Burası DAVRANIŞ DEĞİŞTİRMEZ -
# çalışan koddan TÜRETİLMİŞ bir betimlemedir; alanlar okunarak yazıldı,
# uydurulmadı.
# --------------------------------------------------------------------------- #
class I2cPullupInput(TypedDict, total=False):
    """i2c_pullup girdi sözleşmesi.

    NEREDEN TÜREDİ: ``build_parser()`` — bu dosyada ``--netlist``, ``--segments``,
    ``--json``, ``--self-test`` argümanları (scripts/sch/i2c_pullup.py:495-507).
    Alan adları argparse'ın ürettiği ``Namespace`` alan adlarıdır; bu yüzden
    ``--self-test`` burada ``self_test`` olarak geçer.

    ``segments`` dosyasının KENDİ sözleşmesi ayrıdır: ``i2c-segment-input/v1``
    (bu modülün üst docstring'i, satır 42-60). Burada yalnız o dosyanın YOLU
    taşınır, içeriği değil.

    ``total=False``: ``--self-test`` tek başına geçerli bir çağrıdır; onun
    dışında ``netlist`` ve ``segments`` İKİSİ DE zorunludur ve eksikse
    ``main()`` (satır 511-518) ValueError yükseltir - varsayılan YOKTUR, çünkü
    netlist olmadan pull-up ağı, segment belgesi olmadan pencere VARSAYILMIŞ
    olurdu.

    Çıkış kodları (üst docstring satır 62 + ``common/report_io.status_exit_code``):
    0 = GREEN · 1 = RED veya NO_COVERAGE · 2 = bozuk girdi ya da koşu hatası.
    NO_COVERAGE'ın 1 olması bilerek: "ölçemedim" bir PASS değildir.
    """

    netlist: Path
    segments: Path
    json: Path
    self_test: bool


class I2cPullupOutput(TypedDict):
    """i2c_pullup rapor sözleşmesi (``schema_version`` = ``i2c-pullup/v1``).

    NEREDEN TÜREDİ: ``build_report()`` — bu dosyada raporun kurulduğu sözlük
    (scripts/sch/i2c_pullup.py:462-480). ``schemas/`` altında bu ada karşılık
    gelen bir JSON Schema dosyası YOKTUR; sözleşmenin tek kaynağı o fonksiyondur.

    ``status`` = GREEN | RED | NO_COVERAGE ve ``rollup()`` (satır 447-459)
    tarafından belirlenir: RED, NO_COVERAGE'ı yener - bulunmuş bir kusur
    bulunmuş kalır. ``effective_status`` bu koşuda ``status`` ile aynı yazılır.

    Çıkış kodları: 0 = GREEN · 1 = RED veya NO_COVERAGE · 2 = bozuk girdi ya da
    koşu hatası (``run_cli`` her beklenmedik istisnayı 2'ye çevirir, 1'e değil -
    aksi halde çöken bir araç meşru bir FAIL'den ayırt edilemezdi).
    """

    schema_version: str
    tool: str
    status: str
    effective_status: str
    reason_codes: List[str]
    phase: str
    netlist: dict            # {"path": str|None, "sha256": str|None} - _binding()
    segment_document: dict   # aynı biçim; segment belgesinin bayt bağlaması
    segments: List[dict]     # evaluate_segment() çıktıları
    findings: List[Finding]  # Finding bir dict alt sınıfıdır; doğrudan JSON olur
    coverage: Dict[str, int]  # summarise(): PASS/FAIL/NO_COVERAGE sayıları
    limits: List[str]        # modül düzeyindeki LIMITS listesi


SCHEMA_VERSION = "i2c-pullup/v1"
TOOL_NAME = "i2c_pullup"
INPUT_SCHEMA_VERSION = "i2c-segment-input/v1"

# Field name -> the unit the caller must have written it in. Named units are
# in the KEY, not in a separate field, because a "c_bus" that might be pF or
# F is a factor of 1e12 waiting to happen and no reviewer catches it twice.
SCALARS = (
    ("vdd_v", "V"),
    ("vol_v", "V"),
    ("iol_a", "A"),
    ("c_bus_f", "F"),
    ("t_rise_s", "s"),
)

LIMITS = [
    "resolves pull-ups by TOPOLOGY (a TWO-PIN part bridging the segment and "
    "the declared rail), never by designator prefix - RP1 counts, and a "
    "series resistor on the same net does not",
    "a part with more than two pins that touches both nets is RECORDED, not "
    "modelled: a slave IC with an I/O on SDA and a supply pin on VDD touches "
    "both by construction, and a FET level shifter is indistinguishable from "
    "it here - declare the rail each side of a shifter actually returns to",
    "a two-pin part whose value will not read as a resistance (DNP, a diode, "
    "a ferrite) is NO_COVERAGE, not a skipped item: it sits in the DC path, "
    "so the parallel equivalent is unknown rather than optimistic",
    "one C_bus per segment; it is a DECLARED input, not derived from routing "
    "or from pin counts - declare the worst line's capacitance",
    "one VOL/IOL per segment; when several drivers differ, declare the "
    "WEAKEST one. The tool will not choose for you and does not know which "
    "device on the net is the weak one",
    "the ~0.85 coefficient is DERIVED from the declared measurement band "
    "(ln((1-f_lo)/(1-f_hi))), not stored - so it is attributable to the "
    "clause the caller cited",
    "DC and RC only: no bus timing, no hold time, no arbitration, no clock "
    "stretching, and nothing about whether the segment should have existed",
    "a passing window is not a passing bus - it says the resistor is right, "
    "not that C_bus was measured correctly",
]


# --------------------------------------------------------------------------- #
# Input document
# --------------------------------------------------------------------------- #
def load_segments(path: Path) -> List[dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: input document must be an object")
    segments = doc.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"{path}: 'segments' must be a non-empty array")
    for entry in segments:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: every segment must be an object")
        if not str(entry.get("bus", "")).strip():
            raise ValueError(f"{path}: every segment needs a 'bus' name - the "
                             "identity of the segment is not optional")
        if not str(entry.get("rail_net", "")).strip():
            raise ValueError(f"{entry['bus']}: 'rail_net' is required; the "
                             "pull-up network is defined relative to a rail")
        lines = entry.get("lines")
        if not isinstance(lines, dict) or not lines:
            raise ValueError(f"{entry['bus']}: 'lines' must map a line name "
                             "to its net, e.g. {\"SDA\": \"SENSOR_SDA\"}")
    return segments


# --------------------------------------------------------------------------- #
# The window, and every gap that stops it being computed
# --------------------------------------------------------------------------- #
def read_window_inputs(segment: dict) -> Tuple[dict, List[Gap]]:
    """Read the five scalars plus the rise-time coefficient, with sources."""
    inputs = segment.get("inputs") or {}
    values: Dict[str, object] = {}
    gaps: List[Gap] = []

    if not str(segment.get("mode", "")).strip():
        gaps.append(Gap("mode", MISSING_MODE,
                        "no I²C mode declared; a rise-time limit with no mode "
                        "beside it cannot be audited against the spec"))

    for field, unit in SCALARS:
        value, gap = read_sourced(inputs, field, unit)
        if gap is not None:
            gaps.append(gap)
        else:
            values[field] = value

    band, band_gap = read_rise_time_band(inputs)
    coefficient, coefficient_gap = read_sourced(
        inputs, "rise_time_coefficient", "1")
    if band is not None and coefficient is not None:
        raise ValueError(
            f"{segment.get('bus', '<unnamed segment>')}: declare "
            "rise_time_band OR rise_time_coefficient, not both - two sources "
            "for one number is an unresolved contradiction, not extra evidence")
    if band is not None:
        values["rise_time_band"] = band
    elif coefficient is not None:
        values["rise_time_coefficient"] = coefficient
    else:
        # Report the band's gap: it is the form the matrix note implies, and
        # naming the alternative in the detail keeps the other door visible.
        gap = band_gap if band_gap is not None else coefficient_gap
        gaps.append(Gap(gap.field, gap.code,
                        f"{gap.detail} (declare 'rise_time_band' with the "
                        "fractions the spec measures tr between, or "
                        "'rise_time_coefficient' if the source states it)"))
    return values, gaps


def compute_window(values: dict) -> dict:
    """[Rmin, Rmax] from complete, sourced inputs. Raises on nonsense."""
    if "rise_time_band" in values:
        band = values["rise_time_band"]
        coefficient = rise_time_coefficient(band.low_fraction,
                                            band.high_fraction)
        basis = (f"derived from the declared {band.low_fraction:g}"
                 f"-{band.high_fraction:g} band: "
                 f"ln((1-{band.low_fraction:g})/(1-{band.high_fraction:g}))"
                 f" [{band.source.cite()}]")
    else:
        sourced = values["rise_time_coefficient"]
        coefficient = sourced.value
        basis = f"declared directly [{sourced.source.cite()}]"

    r_min = r_min_ohms(values["vdd_v"].value, values["vol_v"].value,
                       values["iol_a"].value)
    r_max = r_max_ohms(values["t_rise_s"].value, values["c_bus_f"].value,
                       coefficient)
    return {
        "r_min_ohms": r_min,
        "r_max_ohms": r_max,
        "rise_time_coefficient": coefficient,
        "rise_time_coefficient_basis": basis,
        "r_min_formula": "(VDD - VOL) / IOL",
        "r_max_formula": "t_rise / (coefficient * C_bus)",
    }


def _inputs_block(values: dict, gaps: List[Gap]) -> dict:
    """Every electrical input with WHERE it came from, or why it is absent.

    Both halves are in one place on purpose: a report that lists only what
    it had cannot be audited for what it lacked.
    """
    block: Dict[str, object] = {}
    for name, value in sorted(values.items()):
        block[name] = value.to_dict()
    for gap in gaps:
        block.setdefault(gap.field, {"absent": gap.code, "detail": gap.detail})
    return block


# --------------------------------------------------------------------------- #
# One segment
# --------------------------------------------------------------------------- #
def evaluate_segment(nets, components, segment: dict) -> dict:
    """Resolve every line of one segment and judge it against its window."""
    bus = str(segment["bus"])
    rail_net = str(segment["rail_net"])
    values, input_gaps = read_window_inputs(segment)

    window: Optional[dict] = None
    window_error: Optional[str] = None
    if not input_gaps:
        try:
            window = compute_window(values)
        except ValueError as exc:
            # A contradictory but fully sourced input set (VOL above VDD, a
            # negative capacitance) is the caller's document being wrong, not
            # this segment being unmeasured. It must not read as a pass, and
            # it must not read as "we had no data" either.
            window_error = str(exc)

    lines: List[dict] = []
    for line_name in sorted(segment["lines"]):
        net = str(segment["lines"][line_name])
        resolution = resolve_pullups(nets, components, line_name, net, rail_net)
        entry = resolution.to_dict()
        entry["bus"] = bus

        if resolution.gaps:
            entry["status"] = NO_COVERAGE
            entry["verdict"] = None
            entry["detail"] = ("the pull-up network could not be resolved "
                               "from the netlist")
        elif input_gaps:
            entry["status"] = NO_COVERAGE
            entry["verdict"] = None
            entry["detail"] = ("network resolved, but the window has "
                               f"{len(input_gaps)} unsourced or missing "
                               "input(s); no verdict is possible")
        elif window_error is not None:
            entry["status"] = "FAIL"
            entry["verdict"] = "INPUTS_CONTRADICTORY"
            entry["detail"] = window_error
        else:
            verdict = classify(resolution.parallel_ohms,
                               window["r_min_ohms"], window["r_max_ohms"])
            entry["verdict"] = verdict
            entry["status"] = "PASS" if verdict == IN_WINDOW else "FAIL"
            entry["detail"] = VERDICT_DETAIL[verdict]
            if verdict == NO_PULLUP and resolution.other_bridging:
                # The reader's next question is "but what about Q1?", so
                # answer it in the finding instead of in a support thread.
                entry["detail"] += (
                    "; " + ", ".join(f"{ref} ({pins} pins)" for ref, pins
                                     in resolution.other_bridging)
                    + " also touch(es) both nets but are not two-terminal "
                      "parts and are not modelled")
            entry["window_ohms"] = [window["r_min_ohms"], window["r_max_ohms"]]
        lines.append(entry)

    statuses = {entry["status"] for entry in lines}
    if "FAIL" in statuses:
        status = "FAIL"
    elif NO_COVERAGE in statuses or not lines:
        status = NO_COVERAGE
    else:
        status = "PASS"

    return {
        "bus": bus,
        "mode": segment.get("mode"),
        "rail_net": rail_net,
        "status": status,
        "inputs": _inputs_block(values, input_gaps),
        # Kept apart because they are different failures with different
        # owners: an input gap is answered by citing a document, a line gap
        # by fixing the schematic. Rolling them into one list sent the
        # "inputs are unsourced" finding RED on a missing NET.
        "input_gaps": [gap.to_dict() for gap in input_gaps],
        "gaps": [gap.to_dict() for gap in input_gaps]
                + [gap for entry in lines for gap in entry["gaps"]],
        "window": window,
        "window_error": window_error,
        "lines": lines,
    }


# --------------------------------------------------------------------------- #
# Findings and roll-up
# --------------------------------------------------------------------------- #
def build_findings(segments: List[dict]) -> List[Finding]:
    lines = [line for seg in segments for line in seg["lines"]]

    unresolved = [line for line in lines if line["gaps"]]
    resolved = [line for line in lines if not line["gaps"]]
    if not lines:
        resolution_finding = Finding("i2c_pullup_network_resolved",
                                     NO_COVERAGE, 0, [],
                                     "no I²C line was declared")
    elif unresolved:
        resolution_finding = Finding(
            "i2c_pullup_network_resolved", NO_COVERAGE, len(resolved), [],
            f"{len(unresolved)} of {len(lines)} line(s) could not be resolved "
            "from the netlist: " + ", ".join(
                f"{line['bus']}/{line['line']}" for line in unresolved))
    else:
        resolution_finding = result(
            "i2c_pullup_network_resolved", len(resolved), [],
            f"{len(resolved)} line(s) resolved against the netlist")

    complete = [seg for seg in segments if not seg["input_gaps"]]
    if not segments:
        inputs_finding = Finding("i2c_electrical_inputs_sourced",
                                 NO_COVERAGE, 0, [], "no segment was declared")
    elif len(complete) != len(segments):
        inputs_finding = Finding(
            "i2c_electrical_inputs_sourced", NO_COVERAGE, len(complete), [],
            "; ".join(f"{seg['bus']}: {gap['field']} {gap['code']}"
                      for seg in segments for gap in seg["input_gaps"]))
    else:
        inputs_finding = result(
            "i2c_electrical_inputs_sourced", len(segments), [],
            f"{len(segments)} segment(s) carry VDD, VOL, IOL, C_bus, tr and "
            "the rise-time band with a document and revision each")

    judged = [line for line in lines if line["status"] in ("PASS", "FAIL")]
    violations = [{"bus": line["bus"], "line": line["line"],
                   "net": line["net"], "issue": line["verdict"],
                   "parallel_ohms": line["parallel_ohms"],
                   "window_ohms": line.get("window_ohms"),
                   "detail": line["detail"]}
                  for line in judged if line["status"] == "FAIL"]
    window_finding = result(
        "i2c_pullup_window", len(judged), violations,
        f"{len(judged)} line(s) judged against a fully sourced window"
        if judged else "no line had both a resolved network and a sourced "
                       "window; nothing was judged")

    return [resolution_finding, inputs_finding, window_finding]


def rollup(findings: List[Finding], segments: List[dict]
           ) -> Tuple[str, List[str]]:
    """RED beats NO_COVERAGE: a defect that WAS found stays found."""
    counts = summarise(findings)
    if counts.get("FAIL"):
        codes = sorted({line["verdict"] for seg in segments
                        for line in seg["lines"]
                        if line["status"] == "FAIL" and line["verdict"]})
        return "RED", codes
    if counts.get(NO_COVERAGE):
        codes = sorted({gap["code"] for seg in segments for gap in seg["gaps"]})
        return NO_COVERAGE, codes or [MISSING_INPUT]
    return "GREEN", []


def build_report(netlist_path: Path, segments_path: Optional[Path],
                 segments: List[dict]) -> dict:
    findings = build_findings(segments)
    status, reason_codes = rollup(findings, segments)
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "status": status,
        "effective_status": status,
        "reason_codes": reason_codes,
        "phase": "schematic",
        "netlist": _binding(netlist_path),
        "segment_document": _binding(segments_path),
        "segments": segments,
        "findings": findings,
        "coverage": summarise(findings),
        "limits": LIMITS,
    }
    return report


def _binding(path: Optional[Path]) -> dict:
    """Tie the report to the bytes it read, or say loudly that it cannot."""
    if path is None:
        return {"path": None, "sha256": None}
    path = Path(path)
    return {"path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--netlist", type=Path,
                        help="KiCad s-expression netlist; the pull-up network "
                             "is resolved from it and nowhere else")
    parser.add_argument("--segments", type=Path,
                        help=f"segment + sourced input document "
                             f"({INPUT_SCHEMA_VERSION})")
    parser.add_argument("--json", type=Path, help="write the report here")
    parser.add_argument("--self-test", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.netlist is None or args.segments is None:
        raise ValueError(
            "--netlist and --segments are both required. Neither is "
            "defaulted: without the netlist the pull-up network would be "
            "assumed, and without the segment document the window would be")

    nets = read_nets(args.netlist)
    components = read_components(args.netlist)
    declared = load_segments(args.segments)
    segments = [evaluate_segment(nets, components, entry) for entry in declared]
    report = build_report(args.netlist, args.segments, segments)

    diag("")
    diag(f"i2c pull-up: {len(segments)} segment(s) against {args.netlist}")
    for segment in segments:
        for line in segment["lines"]:
            mark = {"PASS": "ok  ", "FAIL": "RED ",
                    NO_COVERAGE: "?   "}[line["status"]]
            parallel = line["parallel_ohms"]
            shown = f"{parallel:.1f} ohm" if parallel is not None else "-"
            diag(f"  {mark} {segment['bus']}/{line['line']:4s} "
                 f"{line['net']:16s} {shown:>12s}  {line['status']}")
            if line["status"] != "PASS":
                diag(f"       {line['detail']}")
        for gap in segment["gaps"]:
            diag(f"  ?    {segment['bus']:16s} {gap['code']}: {gap['detail']}")
    diag(f"  status: {report['status']}")

    write_report(report, args.json)
    emit(report)
    return status_exit_code(report["status"])


def _self_test() -> int:
    """Drives the window into the corners where a permissive tool says yes."""
    # The parallel equivalent is the whole point of the row.
    assert parallel_ohms([1500.0, 1500.0]) == 750.0
    assert parallel_ohms([]) is None, "no pull-up is not 'infinite, near enough'"

    # The 0.85 in the matrix note is the 0.3/0.7 band, not a constant.
    assert abs(rise_time_coefficient(0.3, 0.7) - 0.8472978603872) < 1e-9

    # A bare number carries no provenance and is therefore not an input.
    _, gap = read_sourced({"vdd_v": 3.3}, "vdd_v", "V")
    assert gap is not None and gap.code == "UNSOURCED_INPUT", gap
    # A document with no revision names a moving target.
    _, gap = read_sourced({"vdd_v": {"value": 3.3,
                                     "source": {"document": "X"}}},
                          "vdd_v", "V")
    assert gap is not None and gap.code == "SOURCE_INCOMPLETE", gap

    # An unreadable pull-up value is refused, not silently dropped.
    for text in ("DNP", "", "4.7k 1%"):
        try:
            parse_resistance(text)
        except ValueError:
            pass
        else:  # pragma: no cover - the raise above is the test
            raise AssertionError(f"{text!r} was read as a resistance")
    assert parse_resistance("2k2") == 2200.0
    assert parse_resistance("470R") == 470.0

    # An empty window blames the window, not the fitted resistor.
    assert classify(2200.0, 3000.0, 1000.0) == "WINDOW_EMPTY"

    # A segment whose net is absent from the netlist is NO_COVERAGE.
    nets = parse_nets_fixture()
    resolution = resolve_pullups(nets, {}, "SDA", "GHOST", "+3V3")
    assert resolution.gaps and not resolution.resolved, resolution

    diag("SELF-TEST PASS: parallel equivalent halves, the rise-time "
         "coefficient is derived from the declared band, an unsourced number "
         "is a gap, an unreadable resistor value is refused, and an absent "
         "net is NO_COVERAGE")
    return 0


def parse_nets_fixture():
    """The smallest netlist the self-test needs, built in memory."""
    return parse_nets(
        '(export (version "E") (nets '
        '(net (code "1") (name "+3V3") (node (ref "R1") (pin "2")))))')


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
