#!/usr/bin/env python3
"""ECAD/MCAD thermal bridge: enclosure contact, B.Mask opening, milling slot.

The enclosure is usually treated only as a keep-out threat ("nothing may be
this tall here"). It is also a thermal contact OPPORTUNITY: a heatsink boss
the mechanical engineer left is a path for a hot part's heat to leave the
board. This tool checks that the opportunity is actually usable - bare copper
under the part (a B.Mask opening) and a finish that stays solderable/thermally
stable (ENIG) - sizes the thermal pad, and proposes an Edge.Cuts milling slot
when a heat source and a heat-sensitive part share thick copper.

backend: file  (S-expression text; no pcbnew)

THIS TOOL WRITES THE BOARD
--------------------------
It is the only ported tool that mutates a ``.kicad_pcb``. It therefore
follows the same discipline as ``scripts/sch/rewire.py``:

  1. ``assert_kicad_closed`` first - an open KiCad and a file writer silently
     clobber each other, which is the most destructive failure in the bundle.
  2. a ``.bak`` copy before the first byte is written,
  3. the result is re-read and verified (parenthesis balance, and that every
     proposed block is present); optionally re-verified with ``kicad-cli pcb
     drc``,
  4. any failure restores the ``.bak`` and reports ``rolled_back: true`` with
     ``wrote: false``.

It is deliberately NOT exposed over MCP, for the same reason ``rewire`` is
not: a tool that mutates a design file must be invoked deliberately.

DEFERRED ON PURPOSE
-------------------
The archived module also called the stackup planner's thermal-via count check
(``termal_yonetim_kontrolu``). That check belongs to the stackup tool and is
not duplicated here; the per-via thermal capacity constant it used was itself
documented as "a rough assumption, to be confirmed by FEA or a vendor app
note", and copying an unvalidated constant into a second tool is how two
tools start disagreeing.

Usage:
    thermal_bridge.py <board.kicad_pcb> --thermal thermal.json
                      [--json out.json] [--write] [--force] [--verify-drc]
    thermal_bridge.py --self-test

Exit: 0 = every check measured and clean, 1 = at least one FAIL or
NO_COVERAGE (or a rolled-back write), 2 = error. NO_COVERAGE is non-zero on
purpose: "no hot part sits over a contact zone" and "every thermal contact
checked out" are different statements and must not share an exit code.

TUR 3 ADDITION — first-order junction-temperature screen:

``check_junction_temperature``/``JunctionTempInput`` compute
Tj = T_ref + P * Rtheta from a datasheet RthetaJA/RthetaJC figure (steady
state, single node - see the section's own honesty note). Library-only, like
the rest of this module's calculators; not wired into this file's own CLI
report because a per-component power/Rtheta input has no natural place in the
``--thermal`` JSON shape below - a caller with that data calls it directly.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    result,
    summarise,
)
from common.hashing import sha256_file  # noqa: E402
from common.ids import det_uuid  # noqa: E402
from common.kicad_cli import assert_kicad_closed  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.tuning import ambient_temp_c  # noqa: E402

SCHEMA_VERSION = "thermal-bridge-report/v1"
TOOL_NAME = "thermal_bridge"

# Minimum material left between a milling slot and the board edge. Below this
# the remaining web weakens the board mechanically, which is a vibration and
# solder-crack risk.
MIN_WEB_MM = 1.5


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
@dataclass
class ContactZone:
    """A metal contact face (heatsink boss) derived from the enclosure model."""

    name: str
    polygon: list[tuple[float, float]]
    z_gap_mm: float


@dataclass
class HotComponent:
    """A dissipating component and its thermal-interface state."""

    ref: str
    power_w: float
    x_mm: float
    y_mm: float
    b_mask_opening_defined: bool = False
    surface_finish: str = "TBD"     # ENIG | HASL | TBD


@dataclass
class SourceSensitivePair:
    """A heat source and a heat-sensitive part that may need isolating."""

    source: str
    source_x: float
    source_y: float
    sensitive: str
    sensitive_x: float
    sensitive_y: float
    shared_thick_copper: bool
    distance_to_edge_mm: float


@dataclass
class MillingSlot:
    name: str
    start: tuple[float, float]
    end: tuple[float, float]
    width_mm: float
    web_mm: float


def point_in_polygon(x: float, y: float, polygon) -> bool:
    """Ray-cast containment test."""
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (
                x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1):
            inside = not inside
    return inside


def contact_zone_under(x: float, y: float, zones) -> ContactZone | None:
    """The enclosure contact zone under (x, y), or None.

    No zones at all returns None quietly: no enclosure model shared is a
    coverage gap, reported by the caller as NO_COVERAGE, not an exception.
    """
    for zone in zones:
        if point_in_polygon(x, y, zone.polygon):
            return zone
    return None


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_thermal_contact(components, zones,
                          critical_power_w: float = 0.5) -> Finding:
    """Hot parts sitting over a contact zone need bare copper AND ENIG.

    Only parts above ``critical_power_w`` that actually sit over a zone are
    scanned; a part with nothing above it has no contact requirement, so
    counting it would dilute the coverage number.
    """
    scanned = 0
    violations: list[dict] = []
    for component in components:
        if component.power_w <= critical_power_w:
            continue
        zone = contact_zone_under(component.x_mm, component.y_mm, zones)
        if zone is None:
            continue
        scanned += 1
        if not component.b_mask_opening_defined:
            violations.append({
                "ref": component.ref,
                "zone": zone.name,
                "z_gap_mm": zone.z_gap_mm,
                "problem": "NO_B_MASK_OPENING",
                "message": f"{component.ref} sits over '{zone.name}' but has no "
                           f"B.Mask opening: there is no bare copper to conduct "
                           f"heat into the enclosure.",
            })
        elif component.surface_finish.upper() != "ENIG":
            violations.append({
                "ref": component.ref,
                "zone": zone.name,
                "finish": component.surface_finish,
                "problem": "FINISH_NOT_ENIG",
                "message": f"{component.ref} has a B.Mask opening but the finish "
                           f"is '{component.surface_finish}'. Bare copper oxidises "
                           f"and the thermal interface degrades over time; HASL's "
                           f"unevenness also spoils thermal-pad compression.",
            })
    return result("thermal_contact_usable", scanned, violations,
                  f">{critical_power_w} W parts over an enclosure contact zone "
                  f"need a B.Mask opening and an ENIG finish")


def thermal_pad_thickness_mm(z_gap_mm: float, compression: float = 0.25) -> float:
    """Pad thickness = gap / (1 - compression).

    The pad must compress by ``compression`` (typically 20-30 %) and exactly
    fill the gap. Thinner leaves an air void, which nearly stops conduction;
    thicker over-compresses and puts mechanical stress into the board.
    """
    if z_gap_mm <= 0:
        raise ValueError("z_gap_mm must be positive")
    if not 0 < compression < 1:
        raise ValueError("compression must be in (0, 1)")
    return round(z_gap_mm / (1 - compression), 4)


def thermal_pad_bom_note(name: str, thickness_mm: float) -> str:
    """``thermal_pad_thickness_mm``'s result as a one-line BOM note."""
    return f"Thermal Pad - {thickness_mm}mm ({name})"


# --------------------------------------------------------------------------- #
# Junction temperature - a first-order RθJA/RθJC estimate
# --------------------------------------------------------------------------- #
# HONESTY LIMIT: this is NOT a full parasitic thermal model or an EM/thermal
# solver - only the first-order, steady-state, single-node Tj = T_ref + P * Rθ
# calculation from a datasheet RθJA/RθJC figure. Multi-layer thermal
# resistance networks, transient heating curves and neighbouring-component
# interaction are OUT OF SCOPE - a result near the limit still needs a real
# thermal simulation (e.g. FEA); this function is an early-warning screen only.
@dataclass
class JunctionTempInput:
    """Data a component's first-order thermal estimate NEEDS.

    ``r_theta_ja_c_per_w``/``r_theta_jc_c_per_w`` come from the datasheet -
    never invented here; if both are None the calculation cannot proceed
    (NO_COVERAGE). ``case_contact_temp_c`` is the REAL measured/simulated
    temperature of a ``ContactZone`` the component sits over (this module does
    not produce that number, only consumes it) - when absent, the RθJA/ambient
    path is used instead.
    """

    name: str
    power_w: float
    r_theta_ja_c_per_w: float | None = None
    r_theta_jc_c_per_w: float | None = None
    # None means "use the table": 25 C is the usual DATASHEET reference
    # condition, not this product's enclosure ambient. Keeping it out of
    # the signature means a real enclosure figure replaces it in ONE
    # place before any thermal claim is made.
    ambient_temp_c: float | None = None
    case_contact_temp_c: float | None = None
    max_allowed_tj_c: float | None = None  # datasheet Tj(max) - often ~125C typical, never invented


def junction_temperature_c(data: JunctionTempInput) -> tuple[float | None, str]:
    """``(Tj_degC, method_note)``; ``(None, reason)`` when no Rθ data exists.

    The case-contact path is PREFERRED when both ``case_contact_temp_c`` and
    ``r_theta_jc_c_per_w`` are given (a component thermally bonded to the
    enclosure has the case, not ambient air, as its real reference point -
    consistent with ``check_thermal_contact``'s own assumption). Otherwise
    falls back to RθJA against ambient temperature.
    """
    if data.case_contact_temp_c is not None and data.r_theta_jc_c_per_w is not None:
        tj = data.case_contact_temp_c + data.power_w * data.r_theta_jc_c_per_w
        return tj, (f"RthetaJC={data.r_theta_jc_c_per_w}C/W, "
                    f"case_contact_temp={data.case_contact_temp_c}C")
    if data.r_theta_ja_c_per_w is not None:
        # None means "read the table" - never a silent 25 C.
        ambient = (data.ambient_temp_c if data.ambient_temp_c is not None
                   else ambient_temp_c())
        tj = ambient + data.power_w * data.r_theta_ja_c_per_w
        return tj, (f"RthetaJA={data.r_theta_ja_c_per_w}C/W, "
                    f"ambient_temp={ambient}C")
    return None, "no datasheet RthetaJA/RthetaJC given - Tj cannot be invented"


def check_junction_temperature(data: JunctionTempInput) -> Finding:
    """Wrap ``junction_temperature_c`` as a Finding.

    When ``data.max_allowed_tj_c`` is not given (no datasheet Tj(max) at
    hand), the computed Tj is still reported in ``detail`` but NO PASS/FAIL
    verdict is made - an unbounded "it passed" claim is never invented; the
    result is NO_COVERAGE.
    """
    check = "junction_temperature_first_order"
    tj, method = junction_temperature_c(data)
    if tj is None:
        return result(check, 0, [], f"{data.name}: {method}.")

    if data.max_allowed_tj_c is None:
        return result(
            check, 0, [],
            f"{data.name}: Tj~={tj:.1f}C ({method}) computed, but no datasheet "
            "Tj(max) was given so no PASS/FAIL verdict can be made (NO_COVERAGE "
            "- no limit invented).")

    violations = []
    if tj > data.max_allowed_tj_c:
        violations.append({"name": data.name, "tj_c": round(tj, 2),
                           "max_allowed_tj_c": data.max_allowed_tj_c,
                           "method": method})

    return result(
        check, 1, violations,
        f"{data.name}: Tj~={tj:.1f}C ({method}), limit={data.max_allowed_tj_c}C. "
        "NOTE: first-order estimate - NOT a full parasitic/EM thermal solve.")


def thermal_barrier_needed(pair: SourceSensitivePair) -> bool:
    """A shared thick copper plane means they are not thermally decoupled.

    THERE IS DELIBERATELY NO DISTANCE THRESHOLD. An earlier draft used an
    invented multiplier ("three times the diagonal and you are safe"), which
    is exactly the sort of unsourced numeric physics threshold this bundle
    rejects everywhere else. On a small board a continuous thick plane spreads
    heat across the whole surface quickly, and there is no validated thermal
    resistance data to say where "far enough" begins. Unknown means
    conservative: an extra barrier suggestion, never a missing warning. Real
    measurement or FEA may later justify an exception - with that data, not a
    guess.
    """
    return pair.shared_thick_copper


def suggest_slot(pair: SourceSensitivePair, slot_width_mm: float = 1.0,
                 min_web_mm: float = MIN_WEB_MM) -> MillingSlot | None:
    """An Edge.Cuts milling slot between the two parts, or None.

    None when no barrier is needed, or when the slot would leave less than
    ``min_web_mm`` of material. Winning thermal isolation by losing mechanical
    strength is not a trade this tool makes silently; the caller reports it as
    needing a human.
    """
    if not thermal_barrier_needed(pair):
        return None
    web = pair.distance_to_edge_mm - slot_width_mm
    if web < min_web_mm:
        return None
    mid_x = (pair.source_x + pair.sensitive_x) / 2.0
    y_min = min(pair.source_y, pair.sensitive_y) - 2.0
    y_max = max(pair.source_y, pair.sensitive_y) + 2.0
    return MillingSlot(
        name=f"thermal_barrier_{pair.source}_{pair.sensitive}",
        start=(mid_x, y_min), end=(mid_x, y_max),
        width_mm=slot_width_mm, web_mm=round(web, 4))


def check_thermal_barriers(pairs, slot_width_mm: float = 1.0) -> tuple[Finding, list]:
    """Which source/sensitive pairs need a barrier, and whether one fits."""
    pairs = list(pairs)
    scanned = 0
    violations: list[dict] = []
    slots: list[MillingSlot] = []
    for pair in pairs:
        if not thermal_barrier_needed(pair):
            continue
        scanned += 1
        slot = suggest_slot(pair, slot_width_mm)
        if slot is None:
            violations.append({
                "source": pair.source,
                "sensitive": pair.sensitive,
                "problem": "NO_SLOT_FITS",
                "message": f"{pair.source}/{pair.sensitive} share thick copper and "
                           f"need a thermal barrier, but at "
                           f"{pair.distance_to_edge_mm} mm from the edge no slot "
                           f"leaves the {MIN_WEB_MM} mm minimum web. NEEDS_HUMAN: "
                           f"thermal isolation cannot be had without weakening "
                           f"the board.",
            })
        else:
            slots.append(slot)
    return result("thermal_barrier_resolvable", scanned, violations,
                  "pairs sharing thick copper must get a slot that still leaves "
                  f"a >= {MIN_WEB_MM} mm web"), slots


# --------------------------------------------------------------------------- #
# Board writing
# --------------------------------------------------------------------------- #
def mask_opening_block(board_path: str, name: str, polygon) -> str:
    """A filled ``gr_poly`` on B.Mask - which OPENS the mask there.

    A filled polygon drawn directly on a mask layer is the opening; no zone
    object is needed.
    """
    if len(polygon) < 3:
        raise ValueError("a mask opening polygon needs at least 3 points")
    points = "\n\t\t\t".join(f"(xy {x} {y})" for x, y in polygon)
    uuid = det_uuid("thermal_bridge", board_path, f"mask/{name}")
    return (f"\t(gr_poly\n"
            f"\t\t(pts\n\t\t\t{points}\n\t\t)\n"
            f'\t\t(layer "B.Mask")\n'
            f"\t\t(width 0)\n\t\t(fill yes)\n\t\t(locked no)\n"
            f'\t\t(uuid "{uuid}")\n\t)\n')


def slot_block(board_path: str, slot: MillingSlot) -> str:
    """The two long edges of a milling slot, as Edge.Cuts ``gr_line``s.

    LIMIT: no corner radius. A milling bit cannot cut a sharp inside corner,
    so the fab's minimum slot width and corner radius must be confirmed
    before this geometry is released.
    """
    half = slot.width_mm / 2.0
    (x1, y1), (x2, y2) = slot.start, slot.end
    edges = [((x1 - half, y1), (x2 - half, y2)), ((x1 + half, y1), (x2 + half, y2))]
    out = ""
    for index, (start, end) in enumerate(edges):
        uuid = det_uuid("thermal_bridge", board_path, f"slot/{slot.name}/{index}")
        out += (f"\t(gr_line\n"
                f"\t\t(start {start[0]} {start[1]})\n"
                f"\t\t(end {end[0]} {end[1]})\n"
                f'\t\t(layer "Edge.Cuts")\n'
                f"\t\t(width 0.1)\n"
                f'\t\t(uuid "{uuid}")\n\t)\n')
    return out


def _balanced(text: str) -> bool:
    """Parenthesis balance outside string literals."""
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


def write_blocks(board_path: str, blocks: list[str], *, force: bool = False,
                 verify_drc: bool = False) -> dict:
    """Insert blocks before the closing paren, verify, roll back on failure.

    Returns ``{"wrote", "rolled_back", "backup", "problems"}``.
    """
    board = Path(board_path)
    if not board.is_file():
        raise FileNotFoundError(f"{board_path} not found")
    if not blocks:
        return {"wrote": False, "rolled_back": False, "backup": None,
                "problems": ["nothing to write"]}

    assert_kicad_closed(str(board), force=force)

    original = board.read_text(encoding="utf-8")
    close = original.rfind(")")
    if close == -1 or not _balanced(original):
        raise ValueError(f"{board_path} does not look like a valid .kicad_pcb")

    backup = str(board) + ".bak"
    shutil.copyfile(board, backup)

    updated = original[:close] + "".join(blocks) + original[close:]
    board.write_text(updated, encoding="utf-8")

    problems: list[str] = []
    reread = board.read_text(encoding="utf-8")
    if not _balanced(reread):
        problems.append("the written board no longer has balanced parentheses")
    for block in blocks:
        marker = block.strip().splitlines()[-1]
        if marker not in reread:
            problems.append("a written block is not present in the re-read file")
            break
    if verify_drc and not problems:
        try:
            from common.kicad_cli import KicadCliError, _require_cli
            import subprocess
            import tempfile
            exe = _require_cli()
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "drc.json")
                proc = subprocess.run(
                    [exe, "pcb", "drc", "--format", "json", "-o", out, str(board)],
                    capture_output=True, text=True)
                # A DRC violation is not our problem; a PARSE failure is.
                if proc.returncode not in (0, 1, 5) or not Path(out).is_file():
                    problems.append(
                        f"kicad-cli could not parse the written board: "
                        f"{proc.stderr.strip()}")
        except KicadCliError as exc:
            problems.append(f"kicad-cli unavailable for verification: {exc}")

    if problems:
        shutil.copyfile(backup, board)
        return {"wrote": False, "rolled_back": True, "backup": backup,
                "problems": problems}
    return {"wrote": True, "rolled_back": False, "backup": backup, "problems": []}


# --------------------------------------------------------------------------- #
def _design_block(board_path: str) -> dict:
    """``{path, sha256}`` for the board as it stands right now.

    ``sha256`` is null rather than absent when the file cannot be read: an
    absent field reads as "this tool does not do binding", a null reads as
    "binding was attempted and there was nothing to hash". Only the second
    one is true here.
    """
    path = Path(board_path)
    return {"path": str(path),
            "sha256": sha256_file(path) if path.is_file() else None}


def load_thermal(path: str):
    """Read contact zones, hot components and source/sensitive pairs."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    zones = [ContactZone(z["name"], [tuple(p) for p in z["polygon"]],
                         float(z.get("z_gap_mm", 0.0)))
             for z in data.get("contact_zones", [])]
    components = [HotComponent(
        ref=c["ref"], power_w=float(c["power_w"]),
        x_mm=float(c["x_mm"]), y_mm=float(c["y_mm"]),
        b_mask_opening_defined=bool(c.get("b_mask_opening_defined", False)),
        surface_finish=c.get("surface_finish", "TBD"))
        for c in data.get("components", [])]
    pairs = [SourceSensitivePair(
        source=p["source"], source_x=float(p["source_x"]), source_y=float(p["source_y"]),
        sensitive=p["sensitive"], sensitive_x=float(p["sensitive_x"]),
        sensitive_y=float(p["sensitive_y"]),
        shared_thick_copper=bool(p.get("shared_thick_copper", False)),
        distance_to_edge_mm=float(p.get("distance_to_edge_mm", 0.0)))
        for p in data.get("pairs", [])]
    mask_openings = data.get("mask_openings", [])
    return zones, components, pairs, mask_openings


def self_test() -> list[str]:
    problems: list[str] = []

    if abs(thermal_pad_thickness_mm(0.3) - 0.4) > 1e-9:
        problems.append("pad thickness for a 0.3 mm gap at 25 % is not 0.4 mm")
    if abs(thermal_pad_thickness_mm(0.5, 0.5) - 1.0) > 1e-9:
        problems.append("pad thickness at 50 % compression is wrong")

    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    if not point_in_polygon(5, 5, square) or point_in_polygon(15, 5, square):
        problems.append("point-in-polygon is wrong")

    zones = [ContactZone("boss", square, 0.4)]
    hot = HotComponent("U1", 1.2, 5, 5, b_mask_opening_defined=False)

    # FAULT INJECTION: a hot part over a boss with no mask opening MUST fail.
    finding = check_thermal_contact([hot], zones)
    if finding["status"] != FAIL:
        problems.append("fault injection did not hold: missing mask opening passed")

    # With the opening plus ENIG it passes.
    ok = check_thermal_contact(
        [HotComponent("U1", 1.2, 5, 5, True, "ENIG")], zones)
    if ok["status"] != "PASS":
        problems.append(f"a compliant thermal interface did not PASS: {ok}")

    # HASL is a violation even with the opening.
    hasl = check_thermal_contact(
        [HotComponent("U1", 1.2, 5, 5, True, "HASL")], zones)
    if hasl["status"] != FAIL:
        problems.append("a HASL finish over a contact zone was accepted")

    # No enclosure data -> NO_COVERAGE, never PASS.
    if check_thermal_contact([hot], [])["status"] != "NO_COVERAGE":
        problems.append("no enclosure data did not report NO_COVERAGE")

    # A slot too close to the edge is withheld, not shrunk.
    tight = SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True, 2.0)
    if suggest_slot(tight) is not None:
        problems.append("a slot was proposed that leaves too thin a web")
    roomy = SourceSensitivePair("U1", 0, 0, "Y1", 10, 0, True, 6.0)
    if suggest_slot(roomy) is None:
        problems.append("no slot proposed even though the web is generous")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--thermal",
                        help="JSON with contact_zones, components, pairs, "
                             "mask_openings")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--write", action="store_true",
                        help="write the proposed geometry into the board "
                             "(.bak first, rolled back if verification fails)")
    parser.add_argument("--force", action="store_true",
                        help="skip the KiCad-is-open guard - SCRATCH COPIES ONLY")
    parser.add_argument("--verify-drc", action="store_true",
                        help="after writing, confirm kicad-cli can still parse "
                             "the board")
    parser.add_argument("--critical-power-w", type=float, default=0.5)
    parser.add_argument("--slot-width-mm", type=float, default=1.0)
    parser.add_argument("--compression", type=float, default=0.25)
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: thermal_bridge")
        return 0

    if not args.board:
        parser.error("the board path is required")
    if not args.thermal:
        parser.error("--thermal is required: enclosure contact data is "
                     "caller-supplied, never inferred from the board")

    zones, components, pairs, mask_openings = load_thermal(args.thermal)
    contact = check_thermal_contact(components, zones, args.critical_power_w)
    barrier, slots = check_thermal_barriers(pairs, args.slot_width_mm)
    findings = [contact, barrier]

    proposals: list[dict] = []
    blocks: list[str] = []
    for opening in mask_openings:
        polygon = [tuple(p) for p in opening["polygon"]]
        proposals.append({"kind": "b_mask_opening", "name": opening["name"],
                          "polygon": [list(p) for p in polygon]})
        blocks.append(mask_opening_block(args.board, opening["name"], polygon))
    for slot in slots:
        proposals.append({"kind": "edge_cuts_slot", "name": slot.name,
                          "start": list(slot.start), "end": list(slot.end),
                          "width_mm": slot.width_mm, "web_mm": slot.web_mm})
        blocks.append(slot_block(args.board, slot))

    pad_notes = []
    for zone in zones:
        if zone.z_gap_mm > 0:
            thickness = thermal_pad_thickness_mm(zone.z_gap_mm, args.compression)
            pad_notes.append({"zone": zone.name, "z_gap_mm": zone.z_gap_mm,
                              "pad_thickness_mm": thickness,
                              "bom_note": f"Thermal Pad - {thickness}mm ({zone.name})"})

    write_state = {"wrote": False, "rolled_back": False, "backup": None,
                   "problems": []}
    if args.write:
        write_state = write_blocks(args.board, blocks, force=args.force,
                                   verify_drc=args.verify_drc)
        for problem in write_state["problems"]:
            diag(f"WRITE PROBLEM: {problem}")

    # This tool hand-builds its envelope because it carries extra fields
    # (proposals / wrote / rolled_back) that board_report() does not model.
    # ``design`` and ``metrics`` below are NOT optional extras: without
    # design.sha256 no assertion can tie the report to the board, and without
    # metrics a rule cannot point at one check - `checks` is a list and the
    # selector grammar addresses a.b paths. Hand-built or not, the envelope
    # contract is the same; drifting from it is how a report becomes
    # unreachable while every one of its own tests stays green.
    #
    # The digest is taken AFTER the write step on purpose: this tool mutates
    # the board, so hashing the input would tie the report to a file that no
    # longer exists on disk by the time anyone reads the report.
    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "board": args.board,
        "design": _design_block(args.board),
        "metrics": {f["check"]: len(f["violations"]) for f in findings},
        "wrote": write_state["wrote"],
        "rolled_back": write_state["rolled_back"],
        "backup": write_state["backup"],
        "params": {
            "thermal": args.thermal,
            "critical_power_w": args.critical_power_w,
            "slot_width_mm": args.slot_width_mm,
            "compression": args.compression,
            "min_web_mm": MIN_WEB_MM,
            "thermal_pads": pad_notes,
            "write_problems": write_state["problems"],
        },
        "proposals": proposals,
        "checks": findings,
        "summary": summarise(findings),
    }
    write_report(report, args.json)
    emit(report)

    if args.write and write_state["rolled_back"]:
        diag("ERROR: the write did not verify; the board was restored from .bak")
        return 1
    # This branched on FAIL alone - a hand-rolled copy of
    # common.findings.board_exit_code that never inherited that function's
    # documented (and deliberately frozen) NO_COVERAGE exemption. Both CLI
    # checks report scanned=0 when nothing qualified (no hot part over a
    # contact zone, no pair needing a barrier), and exit 0 there hands the
    # caller a thermal all-clear that was never measured.
    summary = report["summary"]
    return 1 if summary[FAIL] or summary[NO_COVERAGE] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
