#!/usr/bin/env python3
"""DFM evidence from REAL exported RS-274X gerbers - independent of the DRC.

WHY THIS EXISTS
---------------
KiCad's DRC works on the SYMBOLIC model (pad definitions, net-class rules).
"What copper and mask the fab will actually print" is a separate question.
``mask_dam.py`` measures the same solder-mask-dam risk, but from the board
file. This tool reads the ``.gtl``/``.gts``/``.gbs`` that ``kicad-cli pcb
export gerbers`` produced, recovers pad sizes from the aperture definitions
and pad centres from the D03 flash coordinates, and measures the gap from
those real fabrication coordinates. Two independent measurements of the same
risk is the point - agreement is evidence, disagreement is a finding.

backend: file  (pure stdlib; no pcbnew, no kicad-cli)

SCOPE LIMIT - "visual" is not literal
-------------------------------------
There is no raster engine here. Each flash is reduced to the bounding box
implied by its aperture, and the gap between the nearest boxes is measured.
Rotated apertures and true arc-drawn pad edges are approximated by that box,
which is ALWAYS at least as large as the real shape - so a reported gap is
conservative and never optimistic. Unrecognised aperture macros get a zero
size and are EXCLUDED from the gap maths (a zero-size box would fake "lots
of clearance"); they are reported in ``params.parse_warnings``.

O(n^2) pair scan. Fine for a prototype board (~200 flashes, milliseconds);
a dense production panel with thousands of flashes needs a spatial index.
Documented as a boundary, not a TODO.

Usage:
    gerber_dfm.py <gerber-dir-or-file> [--json out.json]
                  [--mask-dam-min-mm 0.20] [--copper-clearance-mm 0.20]
                  [--include-same-net]
    gerber_dfm.py --self-test

Exit: 0 = no FAIL, 1 = at least one FAIL, 2 = run error.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    Finding,
    board_exit_code,
    board_report,
    result,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "gerber-dfm-report/v1"
TOOL_NAME = "gerber_dfm"

GERBER_SUFFIXES = (".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo",
                   ".g2l", ".g3l", ".gm1")


# --------------------------------------------------------------------------- #
# RS-274X parser (validated against real KiCad 10 output)
# --------------------------------------------------------------------------- #
@dataclass
class Aperture:
    """A pad/track shape from one ``%ADDnn...*%`` definition.

    ``width_mm``/``height_mm`` are the BOUNDING BOX of the true shape; corner
    radii and obround ends are not subtracted, so the box always contains the
    real pad.
    """

    code: str
    shape: str          # C | R | O | RoundRect | UNKNOWN
    width_mm: float
    height_mm: float


@dataclass
class Flash:
    """One ``D03`` flash - a pad, via or hole."""

    x_mm: float
    y_mm: float
    aperture: str
    net: str | None = None
    refdes: str | None = None


@dataclass
class Draw:
    """A ``D02``+``D01`` pair - one track segment."""

    start: tuple[float, float]
    end: tuple[float, float]
    aperture: str


@dataclass
class GerberFile:
    apertures: dict[str, Aperture] = field(default_factory=dict)
    flashes: list[Flash] = field(default_factory=list)
    draws: list[Draw] = field(default_factory=list)
    unit: str = "mm"
    file_function: str = ""
    source: str = ""


_AP_SIMPLE = re.compile(r"^%ADD(\d+)([CROP]),([0-9.]+)(?:X([0-9.]+))?")
_AP_ROUNDRECT = re.compile(r"^%ADD(\d+)RoundRect,([0-9.eE+-]+)X")
_FILE_FUNCTION = re.compile(r"^%TF\.FileFunction,([^*]+)\*%")
_MO_MM = re.compile(r"^%MOMM\*%")
_MO_IN = re.compile(r"^%MOIN\*%")
_FORMAT = re.compile(r"^%FSLAX(\d)(\d)Y\d\d\*%")
_D_SELECT = re.compile(r"^D(\d+)\*$")
_COORD = re.compile(r"^(?:X(-?\d+))?(?:Y(-?\d+))?D0([123])\*$")
_TO_NET = re.compile(r"^%TO\.N,([^*]+)\*%")
_TO_PIN = re.compile(r"^%TO\.P,([^,]+),")          # copper: refdes + pin
_TO_COMPONENT = re.compile(r"^%TO\.C,([^*]+)\*%")  # mask/silk: refdes only


def parse_gerber(text: str, source: str = "") -> GerberFile:
    """Parse RS-274X text.

    Supported apertures: ``C`` (circle), ``R`` (rect), ``O`` (obround) and
    KiCad's ``RoundRect`` macro (bounding box only). Anything else is
    recorded as ``UNKNOWN`` with size 0 and excluded from the gap maths -
    see ``parse_warnings``.

    The coordinate scale comes from ``%FSLAX46Y46*%``. That line is mandatory
    in every KiCad gerber; the 1e-6 fallback exists only so a hand-written
    test snippet without a header still parses.
    """
    apertures: dict[str, Aperture] = {}
    flashes: list[Flash] = []
    draws: list[Draw] = []
    scale = 1e-6
    inch = False
    file_function = ""

    current_aperture: str | None = None
    pen: tuple[float, float] | None = None
    net: str | None = None
    refdes: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if _MO_MM.match(line):
            inch = False
            continue
        if _MO_IN.match(line):
            inch = True
            continue

        m = _FORMAT.match(line)
        if m:
            scale = 10 ** (-int(m.group(2)))
            continue

        m = _FILE_FUNCTION.match(line)
        if m:
            file_function = m.group(1)
            continue

        m = _TO_NET.match(line)
        if m:
            net = m.group(1)
            continue
        m = _TO_PIN.match(line) or _TO_COMPONENT.match(line)
        if m:
            refdes = m.group(1)
            continue
        if line == "%TD*%":
            net = refdes = None
            continue

        m = _AP_ROUNDRECT.match(line)
        if m:
            code, radius = m.group(1), float(m.group(2))
            numbers = [float(v) for v in
                       re.findall(r"-?[0-9.]+", line.split(",", 1)[1])]
            xs, ys = numbers[1::2][:4], numbers[2::2][:4]
            if xs and ys:
                apertures[code] = Aperture(
                    code, "RoundRect",
                    round((max(xs) - min(xs)) + 2 * radius, 6),
                    round((max(ys) - min(ys)) + 2 * radius, 6))
            continue

        m = _AP_SIMPLE.match(line)
        if m:
            code, shape, a, b = m.groups()
            size_a = float(a)
            if shape == "C":
                apertures[code] = Aperture(code, "C", size_a, size_a)
            elif shape in ("R", "O"):
                size_b = float(b) if b else size_a
                apertures[code] = Aperture(code, shape, size_a, size_b)
            else:
                apertures[code] = Aperture(code, "UNKNOWN", 0.0, 0.0)
            continue

        m = _D_SELECT.match(line)
        if m and int(m.group(1)) >= 10:
            current_aperture = m.group(1)
            continue

        m = _COORD.match(line)
        if m:
            raw_x, raw_y, code = m.groups()
            unit_scale = scale * (25.4 if inch else 1.0)
            x = float(raw_x) * unit_scale if raw_x else (pen[0] if pen else 0.0)
            y = float(raw_y) * unit_scale if raw_y else (pen[1] if pen else 0.0)
            if code == "2":
                pen = (x, y)
            elif code == "1":
                if pen is not None and current_aperture is not None:
                    draws.append(Draw(pen, (x, y), current_aperture))
                pen = (x, y)
            elif code == "3":
                if current_aperture is not None:
                    flashes.append(Flash(x, y, current_aperture, net, refdes))
                pen = (x, y)

    return GerberFile(apertures, flashes, draws, "mm", file_function, source)


def read_gerber(path: str) -> GerberFile:
    return parse_gerber(Path(path).read_text(encoding="utf-8", errors="replace"),
                        source=str(path))


def parse_warnings(gerber: GerberFile) -> list[str]:
    """Flashes drawn with an unrecognised aperture - excluded from the maths."""
    unknown = {c for c, a in gerber.apertures.items()
               if a.width_mm == 0 and a.height_mm == 0}
    affected = {f.aperture for f in gerber.flashes if f.aperture in unknown}
    return [f"aperture D{code}: unsupported shape, EXCLUDED from the gap scan"
            for code in sorted(affected)]


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def flash_box(flash: Flash, apertures: dict[str, Aperture]):
    """``(x_min, y_min, x_max, y_max)`` in mm, or None when unmeasurable.

    None rather than a zero box: a zero box would read as "infinite
    clearance here" and hide the very defect being looked for.
    """
    aperture = apertures.get(flash.aperture)
    if aperture is None or (aperture.width_mm == 0 and aperture.height_mm == 0):
        return None
    hw, hh = aperture.width_mm / 2.0, aperture.height_mm / 2.0
    return (flash.x_mm - hw, flash.y_mm - hh, flash.x_mm + hw, flash.y_mm + hh)


def box_gap_mm(a, b) -> float:
    """Shortest gap between two AABBs. Negative means they overlap - the same
    convention the DRC uses when clearance goes negative."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx < 0 and dy < 0:
        return max(dx, dy)
    if dx < 0:
        return dy
    if dy < 0:
        return dx
    return (dx * dx + dy * dy) ** 0.5


def close_flash_pairs(gerber: GerberFile, threshold_mm: float,
                      skip_same_net: bool = True) -> list[dict]:
    """Every flash pair closer than ``threshold_mm``.

    ``skip_same_net`` because two pads of the same net sitting close together
    (two VDD pins of one IC) is normal; the real risk is a narrow channel
    between DIFFERENT nets.
    """
    boxes = []
    for flash in gerber.flashes:
        box = flash_box(flash, gerber.apertures)
        if box is not None:
            boxes.append((flash, box))

    found: list[dict] = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            fa, ba = boxes[i]
            fb, bb = boxes[j]
            if skip_same_net and fa.net is not None and fa.net == fb.net:
                continue
            gap = box_gap_mm(ba, bb)
            if gap < threshold_mm:
                found.append({
                    "a": {"x": fa.x_mm, "y": fa.y_mm, "net": fa.net, "ref": fa.refdes},
                    "b": {"x": fb.x_mm, "y": fb.y_mm, "net": fb.net, "ref": fb.refdes},
                    "gap_mm": round(gap, 4),
                })
    return found


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
class WrongLayerError(ValueError):
    """A gerber was handed to a check that expects a different layer."""


def check_mask_dam(mask: GerberFile, min_dam_mm: float,
                   skip_same_net: bool = True) -> Finding:
    """Solder-mask dam width between real mask openings.

    Two openings on different nets left closer than the fab minimum (typically
    0.20-0.25 mm) can flow together during reflow.

    KiCad 10's real ``%TF.FileFunction%`` value is ``Soldermask,Top`` - lower
    case 'm'. A first draft matched ``SolderMask`` and never fired; the real
    exported file caught it immediately. The comparison is case-insensitive
    here so that never happens again.
    """
    if "soldermask" not in mask.file_function.lower():
        raise WrongLayerError(
            f"mask dam check needs a SolderMask layer, got "
            f"{mask.file_function!r} ({mask.source or 'inline'})")
    pairs = close_flash_pairs(mask, min_dam_mm, skip_same_net)
    return result("gerber_mask_dam", len(mask.flashes), pairs,
                  f"layer={mask.file_function}, fab_min={min_dam_mm}mm")


def check_copper_clearance(copper: GerberFile, min_clearance_mm: float,
                           skip_same_net: bool = True) -> Finding:
    """Copper-to-copper gap between real pad/via flashes.

    A second, INDEPENDENT evidence source for short/bridge risk: measured
    from the exported coordinates rather than trusting the symbolic DRC.
    """
    if "copper" not in copper.file_function.lower():
        raise WrongLayerError(
            f"copper clearance check needs a Copper layer, got "
            f"{copper.file_function!r} ({copper.source or 'inline'})")
    pairs = close_flash_pairs(copper, min_clearance_mm, skip_same_net)
    return result("gerber_copper_clearance", len(copper.flashes), pairs,
                  f"layer={copper.file_function}, min_clearance={min_clearance_mm}mm")


def load_gerbers(target: Path) -> list[GerberFile]:
    """Read one gerber file, or every gerber in a directory."""
    if target.is_file():
        return [read_gerber(str(target))]
    files = sorted(p for p in target.iterdir()
                   if p.is_file() and p.suffix.lower() in GERBER_SUFFIXES)
    return [read_gerber(str(p)) for p in files]


def scan(target: Path, min_dam_mm: float, min_clearance_mm: float,
         skip_same_net: bool = True) -> tuple[list[Finding], list[str]]:
    """Run both checks over every recognised layer found under ``target``.

    Layers that are neither mask nor copper are skipped without comment; a
    target with NO usable layer yields NO_COVERAGE findings, not a PASS.
    """
    gerbers = load_gerbers(target)
    warnings: list[str] = []
    mask_findings, copper_findings = [], []
    for gerber in gerbers:
        warnings.extend(f"{Path(gerber.source).name}: {w}"
                        for w in parse_warnings(gerber))
        function = gerber.file_function.lower()
        if "soldermask" in function:
            mask_findings.append(check_mask_dam(gerber, min_dam_mm, skip_same_net))
        elif "copper" in function:
            copper_findings.append(
                check_copper_clearance(gerber, min_clearance_mm, skip_same_net))

    findings = [
        _merge(mask_findings, "gerber_mask_dam",
               f"fab_min={min_dam_mm}mm; no SolderMask layer found"),
        _merge(copper_findings, "gerber_copper_clearance",
               f"min_clearance={min_clearance_mm}mm; no Copper layer found"),
    ]
    return findings, warnings


def _merge(findings: list[Finding], name: str, empty_detail: str) -> Finding:
    """Combine per-layer findings into one, summing coverage."""
    if not findings:
        return Finding(name, "NO_COVERAGE", 0, [], empty_detail)
    scanned = sum(f["scanned"] for f in findings)
    violations = [v for f in findings for v in f["violations"]]
    detail = " | ".join(f["detail"] for f in findings)
    return result(name, scanned, violations, detail)


# --------------------------------------------------------------------------- #
# Self-test (opt-in; stderr only)
# --------------------------------------------------------------------------- #
_SAMPLE = """\
%TF.FileFunction,Copper,L1,Top*%
%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.500000*%
D10*
%TO.N,GND*%
X1000000Y1000000D03*
%TO.N,+3V3*%
X1600000Y1000000D03*
%TD*%
"""


def self_test() -> list[str]:
    """Returns the list of problems; empty means clean."""
    problems: list[str] = []
    gerber = parse_gerber(_SAMPLE)

    if len(gerber.flashes) != 2:
        problems.append(f"expected 2 flashes, got {len(gerber.flashes)}")

    # Two 0.5 mm circles, centres 0.6 mm apart -> gap 0.1 mm.
    pairs = close_flash_pairs(gerber, threshold_mm=0.5)
    if not pairs or abs(pairs[0]["gap_mm"] - 0.1) > 1e-6:
        problems.append(f"expected a 0.1 mm gap, got {pairs}")

    # FAULT INJECTION: with the threshold at 0 nothing may be reported. If
    # something still is, the threshold has no effect and the check is empty.
    if close_flash_pairs(gerber, threshold_mm=0.0) != []:
        problems.append("fault injection did not hold: threshold has no effect")

    # An empty layer must never read as PASS.
    empty = check_copper_clearance(
        parse_gerber("%TF.FileFunction,Copper,L1,Top*%\n"), 0.2)
    if empty["status"] != "NO_COVERAGE":
        problems.append("an empty copper layer did not report NO_COVERAGE")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gerbers", nargs="?",
                        help="exported gerber directory, or a single gerber file")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--mask-dam-min-mm", type=float, default=0.20)
    parser.add_argument("--copper-clearance-mm", type=float, default=0.20)
    parser.add_argument("--include-same-net", action="store_true",
                        help="also report close pairs that share a net")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: gerber_dfm")
        return 0

    if not args.gerbers:
        parser.error("the gerber directory or file is required")

    target = Path(args.gerbers)
    if not target.exists():
        parser.error(f"not found: {args.gerbers}")

    findings, warnings = scan(target, args.mask_dam_min_mm,
                              args.copper_clearance_mm,
                              skip_same_net=not args.include_same_net)
    report = board_report(TOOL_NAME, str(target), findings, params={
        "mask_dam_min_mm": args.mask_dam_min_mm,
        "copper_clearance_mm": args.copper_clearance_mm,
        "skip_same_net": not args.include_same_net,
        "parse_warnings": warnings,
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    for warning in warnings:
        diag(f"WARNING: {warning}")
    return board_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
