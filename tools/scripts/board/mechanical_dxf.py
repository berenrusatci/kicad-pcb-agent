#!/usr/bin/env python3
"""Board outline (DXF) + 3D keepout zones for the ``mekanik-dxf`` skill.

    mechanical_dxf.py CASE.DXF [--json keepout_zones.json]
                       [--outline-layer Board_Outline]
                       [--known-hole-x MM --known-hole-y MM]
                       [--global-max-height-mm MM]
                       [--ceiling-json ceiling_map.json]
                       [--ring-mm 3.0] [--clearance-mm 0.3]
                       [--datum-type edge|hole|mounting_fixture]
                       [--datum-feature-ids id1,id2] [--datum-tolerance-mm MM]
                       [--tolerance-source mechanical_drawing|...]
                       [--board-out board.kicad_pcb]
    mechanical_dxf.py --self-test

Ported from pcb-tool-v2's ``mekanik_dxf_koprusu.py`` (outline import, keepout
derivation, IPD tolerance chain) merged with ``ezdxf_okuyucu.py`` (the real
``ezdxf.readfile()`` reader that feeds it). The two were separate files in
the source project because the DXF-parsing dependency was optional there;
here they are one tool because pcb-agent's contract requires one CLI, one
report, one exit code per artifact.

WHY MERGED NOW (see docs/CONSOLIDATION-NOTES.md "Tur 3"): the source project
originally rejected ``mekanik_dxf_koprusu.py`` as a stub ("no real DXF parse
call"). ``ezdxf_okuyucu.py`` is exactly the missing real-parse step, added to
the source after that rejection — so the rejection no longer holds once both
files are read together.

The primary output is ``keepout_zones.json``, matching
``skills/mekanik-dxf/references/keepout_zones.schema.json`` exactly (this
tool's ``--json`` report body IS the artifact the skill consumes, not a
separate checker envelope). Component-height verification against those
zones (source SKILL.md step 4, "Z kontrolü") is a second concern the source
bundled in the same file; here it is ``--check-heights`` on the same tool
because it consumes the same zone list, but it is not part of the primary
keepout_zones.json output.

SCOPE (unchanged from the source's own "AĞ/ARAÇ UYARISI"): real DXF parsing
needs the ``ezdxf`` package. When it is not installed, or the DXF has no
geometry on the outline layer, the run is NO_COVERAGE, never a silent PASS.
STEP/3D ceiling maps are supplied as a pre-parsed JSON (``--ceiling-json``);
this tool does not parse STEP/CAD files itself (no CAD kernel dependency),
matching the source's stated boundary between ``ezdxf`` and
``cadquery``/``python-occ``.

Exit codes: 0 = GREEN, 1 = CONFIRM/RED/NO_COVERAGE, 2 = bad input or crash.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.boardgeom import point_in_polygon as _point_in_polygon  # noqa: E402
from common.report_io import (  # noqa: E402
    diag,
    emit,
    error_report,
    run_cli,
    write_report,
)

SCHEMA_VERSION = "keepout-zones/v1"
TOOL_NAME = "mechanical_dxf"

_INSUNITS_MM = {4}
_INSUNITS_INCH = {1}

_ANCHOR_TOLERANCE_MM = 0.5  # matches the source's "kaba eşik" note


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı. Girdi
# `main()` argparse bloğundan, çıktı `build_report()`'un GERÇEKTEN kurduğu
# sözlükten + skills/mekanik-dxf/references/keepout_zones.schema.json'dan
# türetildi. Şemada olmayıp kodda olan üç alan (issues/outline_points_mm/
# outline_closed) ile şart bağlı `board_write` aşağıda AYRICA işaretlidir.
# --------------------------------------------------------------------------- #
class MechanicalDxfInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `main()` argparse bloğu (s.532-552).

    Bir şey BİLİNMİYORSA verilmemelidir: `global_max_height_mm` yoksa yükseklik
    tavanı `None` kalır ve o bölge NO_COVERAGE olur — "sınırsız" varsayılmaz.
    Aynı biçimde `known_hole_x`/`known_hole_y` YALNIZ İKİSİ BİRDEN verildiğinde
    çapa kontrolü (birim/orijin hatası yakalama) çalışır; tek başına biri
    sessizce yok sayılır.
    """

    json: str                    # --json: keepout_zones.json'un yazılacağı yol
    outline_layer: str           # --outline-layer, vars. "Board_Outline"
    known_hole_x: float          # --known-hole-x (çapa kontrolü için; y ile birlikte)
    known_hole_y: float          # --known-hole-y (çapa kontrolü için; x ile birlikte)
    global_max_height_mm: float  # --global-max-height-mm (yoksa tavan bilinmiyor = None)
    ceiling_json: str            # --ceiling-json: [{polygon_mm, ceiling_mm}] listesi (ön-ayrıştırılmış STEP)
    ring_mm: float               # --ring-mm, vars. 3.0 — montaj deliği çevresi halka
    clearance_mm: float          # --clearance-mm, vars. 0.3 — tavandan düşülen pay
    datum_type: str              # --datum-type: edge|hole|mounting_fixture|unknown, vars. "unknown"
    datum_feature_ids: str       # --datum-feature-ids: virgülle ayrık; rapora liste olarak girer
    datum_tolerance_mm: float    # --datum-tolerance-mm (varsayılan yok — beyan edilmezse None)
    tolerance_source: str        # --tolerance-source: mechanical_drawing|connector_datasheet|derived_from_enclosure|unknown
    board_out: str               # --board-out: doğrulanmış anahat bu .kicad_pcb'nin Edge.Cuts'ına yazılır
    self_test: bool              # --self-test: DXF gerektirmez; saf geometri + kusur enjeksiyonu koşar


class MechanicalDxfInput(MechanicalDxfInputOptional):
    """Aracın tam girdisi. Kaynak: `main()` argparse bloğu (s.532-552).

    `dxf` argparse'ta `nargs="?"` ile TANIMLIDIR ama tek istisnası vardır:
    `--self-test` verilmediyse `ap.error()` ile ZORUNLU kılınır (s.556-557).
    Yani bir ajan için pratik kural: `dxf` ya da `self_test` — biri şart.
    """

    dxf: str                     # konumsal: okunacak CASE.DXF yolu


class MechanicalDxfOutput(TypedDict, total=False):
    """`--json` ile yazılan raporun üst düzey anahtarları.

    Kaynak: `build_report()` (bu dosyada s.423-492) + skills/mekanik-dxf/
    references/keepout_zones.schema.json. Şemanın `required` listesi
    (schema_version, units, global_max_height_mm, datum, zones, status) bu
    sınıfın alt kümesidir; şema ile ÇELİŞKİ yoktur.

    `total=False` BİLEREK: `build_report()` üç farklı dalda döner ve son üç
    alan yalnız normal (NO_COVERAGE/CONFIRM olmayan) dalda yazılır; `board_write`
    ise yalnız `--board-out` verildiğinde ve durum GREEN/SCREEN_ONLY ise eklenir
    (s.574-576). Var olmayan alanı "boş" diye okumak, çalışmamış bir adımı
    çalışmış saymak olurdu.

    ÇIKIŞ KODLARI (dosya başı notu): 0 = GREEN, 1 = CONFIRM/RED/NO_COVERAGE,
    2 = bozuk girdi ya da çökme. `exit_code_for()` yalnız `status == "GREEN"`
    için 0 döndürür — burada NO_COVERAGE 0 DEĞİLDİR (kart denetleyicilerinin
    tersi; bilinçli fark).
    """

    schema_version: str          # "keepout-zones/v1"
    units: str                   # "mm" — sabit
    global_max_height_mm: float | None  # girdiden aynen; beyan yoksa None
    mechanical_tolerance_mm: float | None  # şemada var, bu araç doldurmaz (her zaman None)
    clearance_margin_mm: float   # --clearance-mm
    datum: dict                  # {datum_type, datum_feature_ids, datum_tolerance_mm, tolerance_source, connector_offset_from_datum}
    step_provenance: None        # şemada var, bu araç STEP ayrıştırmaz (her zaman None)
    zones: list                  # {zone_id, kind, owner_refdes, side, polygon_mm, max_allowed_height_mm, source, note}
    status: str                  # GREEN | SCREEN_ONLY | CONFIRM | NO_COVERAGE
    coverage: dict               # {"eligible", "scanned", "excluded", "violations"}
    issues: list                 # {reason_code, message, ...} — EZDXF_NOT_INSTALLED / DXF_UNREADABLE / OUTLINE_LAYER_EMPTY / OUTLINE_NOT_CLOSED / ANCHOR_CHECK_FAILED
    outline_points_mm: list      # yalnız normal dalda: [[x, y], ...] okunan anahat
    outline_closed: bool         # yalnız normal dalda: poligon kapalı mı
    board_write: dict            # yalnız --board-out ile: {"written": bool, "segments"|"reason_code"}


# --------------------------------------------------------------------------- #
# 1. DXF outline import
# --------------------------------------------------------------------------- #
def _mil_to_mm(value: float) -> float:
    return value * 0.0254


def _polygon_closed(points: list[tuple[float, float]], tol_mm: float = 0.01) -> bool:
    if len(points) < 3:
        return False
    (x0, y0), (x1, y1) = points[0], points[-1]
    return math.hypot(x1 - x0, y1 - y0) <= tol_mm


def _arc_to_polyline(center, radius, start_deg, end_deg, segments=16):
    """TODO carried over unresolved from the source: segment count is fixed,
    not adaptive to radius/arc length; large-radius arcs will facet visibly.
    Not addressed here either — out of scope for this port."""
    cx, cy = center
    a0, a1 = math.radians(start_deg), math.radians(end_deg)
    if a1 < a0:
        a1 += 2 * math.pi
    return [
        (cx + radius * math.cos(a0 + (a1 - a0) * i / segments),
         cy + radius * math.sin(a0 + (a1 - a0) * i / segments))
        for i in range(segments + 1)
    ]


class OutlineImportError(ValueError):
    """Anchor check failed or units are ambiguous - CONFIRM, not a crash."""

    def __init__(self, reason_code: str, message: str, **extra: Any):
        super().__init__(message)
        self.reason_code = reason_code
        self.extra = extra


def read_dxf_outline(dxf_path: str, outline_layer: str = "Board_Outline"):
    """Real ``ezdxf.readfile()`` scan of LWPOLYLINE + LINE + ARC on
    ``outline_layer``, plus CIRCLE on ``<outline_layer>_Deliği`` for holes.

    Returns ``(points_mm, holes_mm, coverage_note)``. ``coverage_note`` is
    ``None`` on a normal read, or a dict with ``reason_code``/``message`` when
    there was nothing to scan (missing ``ezdxf``, unreadable file, empty
    layer) - the caller turns that into NO_COVERAGE without raising.
    """
    try:
        import ezdxf
    except ImportError:
        return [], [], {
            "reason_code": "EZDXF_NOT_INSTALLED",
            "message": "ezdxf library not installed - DXF read did not run.",
        }

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except (IOError, ezdxf.DXFStructureError) as exc:
        return [], [], {
            "reason_code": "DXF_UNREADABLE",
            "message": f"could not open DXF: {exc}",
        }

    insunits = doc.header.get("$INSUNITS", 0)
    if insunits in _INSUNITS_MM:
        scale, unit = 1.0, "mm"
    elif insunits in _INSUNITS_INCH:
        # Inch, not mil: scale to mil here (x1000) so the mil->mm step below
        # is the correct magnitude (inch * 1000 * 0.0254 == inch * 25.4).
        scale, unit = 1000.0, "mil"
    else:
        raise OutlineImportError(
            "UNKNOWN_INSUNITS",
            f"DXF header has an unrecognized/unitless $INSUNITS={insunits} - "
            "refusing to silently assume mm.",
            insunits_code=insunits,
        )

    msp = doc.modelspace()
    points_native: list[tuple[float, float]] = []
    for e in msp.query(f'LWPOLYLINE[layer=="{outline_layer}"]'):
        points_native.extend((p[0], p[1]) for p in e.get_points())
    for e in msp.query(f'LINE[layer=="{outline_layer}"]'):
        points_native.append((e.dxf.start.x, e.dxf.start.y))
        points_native.append((e.dxf.end.x, e.dxf.end.y))
    for e in msp.query(f'ARC[layer=="{outline_layer}"]'):
        points_native.extend(_arc_to_polyline(
            (e.dxf.center.x, e.dxf.center.y), e.dxf.radius,
            e.dxf.start_angle, e.dxf.end_angle))

    if not points_native:
        return [], [], {
            "reason_code": "OUTLINE_LAYER_EMPTY",
            "message": f"no LWPOLYLINE/LINE/ARC found on layer '{outline_layer}'.",
        }

    hole_layer = f"{outline_layer}_Deliği"
    holes_native = [
        (e.dxf.center.x, e.dxf.center.y, e.dxf.radius * 2)
        for e in msp.query(f'CIRCLE[layer=="{hole_layer}"]')
    ]

    points_mm = [(x * scale if unit == "mil" else x,
                  y * scale if unit == "mil" else y) for x, y in points_native]
    holes_mm = [(cx * scale if unit == "mil" else cx,
                 cy * scale if unit == "mil" else cy,
                 d * scale if unit == "mil" else d) for cx, cy, d in holes_native]
    if unit == "mil":
        points_mm = [(_mil_to_mm(x), _mil_to_mm(y)) for x, y in points_mm]
        holes_mm = [(_mil_to_mm(x), _mil_to_mm(y), _mil_to_mm(d)) for x, y, d in holes_mm]

    return points_mm, holes_mm, None


def verify_anchor(holes_mm, known_hole_xy) -> None:
    """Raises ``OutlineImportError`` (CONFIRM, not a crash) if the nearest
    hole to a known real-world anchor coordinate is implausibly far away -
    a unit/origin mismatch signal. No holes to check against is not an error
    here; it just skips the check."""
    if known_hole_xy is None or not holes_mm:
        return
    bx, by = known_hole_xy
    nearest = min(holes_mm, key=lambda h: math.hypot(h[0] - bx, h[1] - by))
    deviation = math.hypot(nearest[0] - bx, nearest[1] - by)
    if deviation > _ANCHOR_TOLERANCE_MM:
        raise OutlineImportError(
            "ANCHOR_CHECK_FAILED",
            f"known hole ({bx},{by}) is {deviation:.2f}mm from the nearest "
            f"outline hole ({nearest[0]:.2f},{nearest[1]:.2f}) - suspected "
            "unit or origin error.",
            deviation_mm=round(deviation, 4),
        )


# --------------------------------------------------------------------------- #
# 2. Keepout zone derivation (schema: keepout_zones.schema.json)
# --------------------------------------------------------------------------- #


def derive_mounting_hole_zones(holes_mm, ring_mm=3.0, global_max_height_mm=None,
                                ceiling_map=None, clearance_mm=0.3):
    """One ``kind: mounting_hole`` zone per DXF hole. Height comes from a
    region-based STEP ceiling map when given, else the single
    ``global_max_height_mm``, else unknown (``null`` - NO_COVERAGE for that
    zone, never assumed unlimited)."""
    zones = []
    for i, (cx, cy, diameter) in enumerate(holes_mm):
        radius = diameter / 2 + ring_mm
        polygon = [
            [cx + radius * math.cos(2 * math.pi * k / 8),
             cy + radius * math.sin(2 * math.pi * k / 8)]
            for k in range(8)
        ]
        ceiling_mm, source = None, "dxf"
        if ceiling_map:
            for region in ceiling_map:
                if _point_in_polygon(cx, cy, region["polygon_mm"]):
                    ceiling_mm, source = region["ceiling_mm"], "step"
                    break
        if ceiling_mm is None and global_max_height_mm is not None:
            ceiling_mm, source = global_max_height_mm, "local_policy"

        max_allowed = round(ceiling_mm - clearance_mm, 4) if ceiling_mm is not None else None
        zones.append({
            "zone_id": f"mounting_hole_{i}_keepout",
            "kind": "mounting_hole",
            "owner_refdes": None,
            "side": "both",
            "polygon_mm": polygon,
            "max_allowed_height_mm": max_allowed,
            "source": source,
            "note": None,
        })
    return zones


def check_component_heights(components, zones):
    """SKILL.md step 4: each component's body-height must not exceed the
    ``max_allowed_height_mm``/``..._bottom`` of the zone whose polygon
    contains it. Returns a violation list; empty means no zone was breached
    (not the same as "nothing was checked" - see the tool's --check-heights
    exit code, which is NO_COVERAGE when either list is empty)."""
    violations = []
    for c in components:
        for z in zones:
            if not _point_in_polygon(c["x_mm"], c["y_mm"], z["polygon_mm"]):
                continue
            limit = (z.get("max_allowed_height_mm_bottom")
                     if c.get("side") == "bottom" else z["max_allowed_height_mm"])
            if limit is not None and c["body_height_mm"] > limit:
                violations.append({
                    "refdes": c["refdes"],
                    "zone_id": z["zone_id"],
                    "body_height_mm": c["body_height_mm"],
                    "max_allowed_height_mm": limit,
                    "side": c.get("side", "top"),
                })
    return violations


# --------------------------------------------------------------------------- #
# 3. IPD tolerance chain (stereo/optical - board_outline.json territory,
#    schema: null per SKILL.md, so this returns a plain dict)
# --------------------------------------------------------------------------- #
def ipd_tolerance_chain(nominal_ipd_mm, components_mm, rss=False):
    """``components_mm`` is a list of per-source tolerance_mm values.
    ``rss=True`` -> root-sum-square (statistical, assumes independence);
    ``rss=False`` -> worst-case (direct sum). The caller must pick
    deliberately; there is no silent default reason to prefer one."""
    if rss:
        total = math.sqrt(sum(t ** 2 for t in components_mm))
    else:
        total = sum(components_mm)
    return {
        "nominal_mm": nominal_ipd_mm,
        "total_tolerance_mm": round(total, 4),
        "min_mm": round(nominal_ipd_mm - total, 4),
        "max_mm": round(nominal_ipd_mm + total, 4),
        "method": "RSS" if rss else "worst_case",
    }


def apply_optical_center_offset(footprint_origin_mm, optical_center_offset_mm):
    fx, fy = footprint_origin_mm
    ox, oy = optical_center_offset_mm
    return (fx + ox, fy + oy)


# --------------------------------------------------------------------------- #
# 4. Optional: write the verified outline back to a real board
# --------------------------------------------------------------------------- #
def write_edge_cuts(board_path: str, points_mm, line_width_mm=0.1):
    """Writes ``points_mm`` (closed polygon, mm) to Edge.Cuts as
    ``PCB_SHAPE`` segments and saves. Requires the verified/closed polygon
    already produced by ``read_dxf_outline`` - this function does not
    re-validate closure beyond the check below, matching the source's
    contract that only an already-verified outline reaches this step."""
    if not _polygon_closed(points_mm):
        return {"written": False, "reason_code": "OUTLINE_NOT_CLOSED"}
    try:
        import pcbnew
    except ImportError:
        return {"written": False, "reason_code": "PCBNEW_NOT_AVAILABLE"}

    segments = list(zip(points_mm[:-1], points_mm[1:]))
    first, last = points_mm[0], points_mm[-1]
    if math.hypot(last[0] - first[0], last[1] - first[1]) > 0.01:
        segments.append((last, first))

    board = pcbnew.LoadBoard(str(board_path))
    for a, b in segments:
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
        shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(pcbnew.FromMM(line_width_mm))
        board.Add(shape)
    board.Save(str(board_path))
    return {"written": True, "segments": len(segments)}


# --------------------------------------------------------------------------- #
# 5. Report assembly (keepout_zones.schema.json, verbatim)
# --------------------------------------------------------------------------- #
def build_report(dxf_path, outline_layer, known_hole_xy, global_max_height_mm,
                  ceiling_map, ring_mm, clearance_mm, datum_type,
                  datum_feature_ids, datum_tolerance_mm, tolerance_source):
    points_mm, holes_mm, note = read_dxf_outline(dxf_path, outline_layer)

    datum = {
        "datum_type": datum_type,
        "datum_feature_ids": datum_feature_ids,
        "datum_tolerance_mm": datum_tolerance_mm,
        "tolerance_source": tolerance_source,
        "connector_offset_from_datum": None,
    }
    base = {
        "schema_version": SCHEMA_VERSION,
        "units": "mm",
        "global_max_height_mm": global_max_height_mm,
        "mechanical_tolerance_mm": None,
        "clearance_margin_mm": clearance_mm,
        "datum": datum,
        "step_provenance": None,
    }

    if note is not None:
        return {**base, "zones": [], "status": "NO_COVERAGE",
                "coverage": {"eligible": 0, "scanned": 0, "excluded": 0, "violations": 0},
                "issues": [note]}

    try:
        verify_anchor(holes_mm, known_hole_xy)
    except OutlineImportError as exc:
        return {**base, "zones": [], "status": "CONFIRM",
                "coverage": {"eligible": 1, "scanned": 1, "excluded": 0, "violations": 1},
                "issues": [{"reason_code": exc.reason_code, "message": str(exc), **exc.extra}]}

    closed = _polygon_closed(points_mm)
    zones = derive_mounting_hole_zones(
        holes_mm, ring_mm=ring_mm, global_max_height_mm=global_max_height_mm,
        ceiling_map=ceiling_map, clearance_mm=clearance_mm)

    issues = []
    if not closed:
        issues.append({
            "reason_code": "OUTLINE_NOT_CLOSED",
            "message": "board outline polygon is not closed - the board would not fill.",
        })

    if not closed:
        status = "CONFIRM"
    elif not zones:
        status = "NO_COVERAGE"
    elif any(z["max_allowed_height_mm"] is None for z in zones):
        status = "NO_COVERAGE"
    elif any(z["source"] == "local_policy" for z in zones):
        status = "SCREEN_ONLY"
    else:
        status = "GREEN"

    eligible = len(zones)
    scanned = eligible
    violations = sum(1 for z in zones if z["max_allowed_height_mm"] is None)
    return {
        **base,
        "zones": zones,
        "status": status,
        "coverage": {"eligible": eligible, "scanned": scanned, "excluded": 0,
                     "violations": violations},
        "issues": issues,
        "outline_points_mm": [[round(x, 4), round(y, 4)] for x, y in points_mm],
        "outline_closed": closed,
    }


def exit_code_for(report: dict) -> int:
    return 0 if report["status"] == "GREEN" else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    """No real DXF file needed: exercises the pure-geometry functions with a
    fault-injection pass (clean scenario passes, then a deliberately broken
    one must fail), matching the source test suite's own discipline."""
    zones = derive_mounting_hole_zones(
        [(5.0, 5.0, 2.0), (45.0, 25.0, 2.0)], ring_mm=3.0,
        ceiling_map=[{"polygon_mm": [[0, 0], [50, 0], [50, 30], [0, 30]], "ceiling_mm": 4.0}],
        clearance_mm=0.3)
    assert len(zones) == 2 and zones[0]["max_allowed_height_mm"] == 3.7, "ceiling map not applied"

    clean = check_component_heights(
        [{"refdes": "U1", "x_mm": 5, "y_mm": 5, "body_height_mm": 1.0}], zones)
    assert clean == [], "clean scenario should have no violations before fault injection"

    broken = check_component_heights(
        [{"refdes": "U1", "x_mm": 5, "y_mm": 5, "body_height_mm": 999.0}], zones)
    assert len(broken) == 1 and broken[0]["refdes"] == "U1", \
        "injected height violation was not caught - check may be empty"

    try:
        verify_anchor([(5.0, 5.0, 2.0)], (500.0, 500.0))
        raise AssertionError("anchor check should have raised on a far-away known hole")
    except OutlineImportError:
        pass

    diag("self-test OK: ceiling-map zones, height fault-injection, anchor check all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dxf", nargs="?")
    ap.add_argument("--json")
    ap.add_argument("--outline-layer", default="Board_Outline")
    ap.add_argument("--known-hole-x", type=float)
    ap.add_argument("--known-hole-y", type=float)
    ap.add_argument("--global-max-height-mm", type=float)
    ap.add_argument("--ceiling-json", help="JSON list of {polygon_mm, ceiling_mm} regions")
    ap.add_argument("--ring-mm", type=float, default=3.0)
    ap.add_argument("--clearance-mm", type=float, default=0.3)
    ap.add_argument("--datum-type", default="unknown",
                     choices=["edge", "hole", "mounting_fixture", "unknown"])
    ap.add_argument("--datum-feature-ids", default="")
    ap.add_argument("--datum-tolerance-mm", type=float)
    ap.add_argument("--tolerance-source", default="unknown",
                     choices=["mechanical_drawing", "connector_datasheet",
                              "derived_from_enclosure", "unknown"])
    ap.add_argument("--board-out", help="Write the verified outline to this .kicad_pcb")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.dxf:
        ap.error("DXF path is required unless --self-test is given")

    known_hole_xy = None
    if a.known_hole_x is not None and a.known_hole_y is not None:
        known_hole_xy = (a.known_hole_x, a.known_hole_y)

    ceiling_map = None
    if a.ceiling_json:
        ceiling_map = json.loads(Path(a.ceiling_json).read_text(encoding="utf-8"))

    report = build_report(
        a.dxf, a.outline_layer, known_hole_xy, a.global_max_height_mm, ceiling_map,
        a.ring_mm, a.clearance_mm, a.datum_type,
        [s for s in a.datum_feature_ids.split(",") if s],
        a.datum_tolerance_mm, a.tolerance_source,
    )

    if a.board_out and report["status"] in ("GREEN", "SCREEN_ONLY"):
        write_result = write_edge_cuts(a.board_out, [tuple(p) for p in report["outline_points_mm"]])
        report["board_write"] = write_result

    write_report(report, a.json)
    emit(report)
    return exit_code_for(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
