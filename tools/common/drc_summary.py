"""Geographic clustering of DRC/ERC violations into a readable summary.

Ported from ``drc_ozetleyici.py``. A raw KiCad DRC report dumps every
violation as its own ``[severity] description`` line; a dense area (say
twelve shorts under one IC) becomes twelve lines that bloat the context and
HIDE the location context — is the damage in one spot, or spread over the
whole board?

This module reads the coordinates that the real KiCad DRC JSON schema
carries under ``items[].pos`` for every violation, clusters them on a simple
grid, and reports the result as a ``common.findings.Finding``.

DESIGN: this module takes a plain report ``dict`` and nothing else. It does
not import a KiCad bridge and does not run DRC itself, so it can be tested
without KiCad. The one optional exception is ``nearest_footprint``, which
shells out to KiCad's embedded Python (see its docstring) and returns None
on any failure.

Library only: no CLI, no stdout writes.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .findings import Finding, result

# Same schema assumption as the KiCad bridge: violations arrive under two
# separate keys. Kept as a local copy on purpose — this module is deliberately
# "takes only a dict" and imports no bridge.
VIOLATION_KEYS = ("violations", "unconnected_items")


def all_violations(report: dict) -> list[dict]:
    """Every violation in the report, from both schema keys."""
    out: list[dict] = []
    for key in VIOLATION_KEYS:
        out.extend(report.get(key, []))
    return out


def representative_position(violation: dict) -> tuple[float, float] | None:
    """Centroid of the ``pos`` fields in a violation's ``items[]``.

    Returns None when ``items``/``pos`` are missing (an unexpected or
    incomplete input schema) — NO COORDINATE IS INVENTED. The caller must put
    such a violation in the "unlocated" group rather than dropping it.
    """
    points = [
        (float(item["pos"]["x"]), float(item["pos"]["y"]))
        for item in violation.get("items", [])
        if isinstance(item, dict) and isinstance(item.get("pos"), dict)
        and "x" in item["pos"] and "y" in item["pos"]
    ]
    if not points:
        return None
    return (
        sum(x for x, _ in points) / len(points),
        sum(y for _, y in points) / len(points),
    )


_BRACKETED_NET_NAME = re.compile(r"\[([^\]]+)\]")


def unconnected_net_positions(report: dict) -> dict[str, list[tuple[float, float]]]:
    """Every ``unconnected_items`` entry's item positions, grouped by net.

    Feeds a route-request generator: pair up two positions on the same net
    and hand them to a router as a from/to request.

    NET NAME EXTRACTION: real kicad-cli DRC JSON has not been observed to
    carry a top-level ``net_name`` field on ``unconnected_items`` at all - the
    net name instead appears bracketed inside each ITEM's own ``description``
    (observed shape: ``"... [IMU_INT1] U1 pad F.Cu"``). Each item's own
    bracketed name is tried first; the violation-level ``net_name``/
    ``description`` is only a fallback for items whose description does not
    match, and no name is ever fabricated.
    """
    groups: dict[str, list[tuple[float, float]]] = {}
    for violation in report.get("unconnected_items", []):
        fallback = violation.get("net_name") or violation.get(
            "description", "UNKNOWN_NET")
        for item in violation.get("items", []):
            pos = item.get("pos")
            if not (isinstance(pos, dict) and "x" in pos and "y" in pos):
                continue
            match = _BRACKETED_NET_NAME.search(item.get("description", ""))
            net = match.group(1) if match else fallback
            groups.setdefault(net, []).append(
                (float(pos["x"]), float(pos["y"])))
    return groups


@dataclass
class Cluster:
    """One group of geographically close violations."""

    center: tuple[float, float] | None
    count: int
    severity_counts: dict[str, int] = field(default_factory=dict)
    sample_descriptions: list[str] = field(default_factory=list)
    refdes: str | None = None

    @property
    def unlocated(self) -> bool:
        return self.center is None


def _tally(violations: list[dict]) -> tuple[dict[str, int], list[str]]:
    severity_counts: dict[str, int] = {}
    descriptions: list[str] = []
    for violation in violations:
        severity = violation.get("severity", "?")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        descriptions.append(violation.get("description", ""))
    return severity_counts, descriptions


def cluster_violations(violations: list[dict],
                       cell_size_mm: float = 2.0) -> list[Cluster]:
    """Grid clustering: each violation's representative position is rounded
    onto a ``cell_size_mm`` grid cell, and violations landing in the same cell
    become ONE ``Cluster``. The cluster centre is the MEAN of the real
    positions in that cell, not the cell corner, so the "it is here" marker
    is as accurate as the input allows.

    Unlocated violations (``representative_position`` returned None) are NOT
    silently dropped — they are collected in their own ``unlocated`` cluster.
    """
    if cell_size_mm <= 0:
        raise ValueError(f"cell_size_mm > 0 olmalı, alındı: {cell_size_mm}")

    cells: dict[object, list[tuple[dict, tuple[float, float]]]] = {}
    unlocated: list[dict] = []

    for violation in violations:
        position = representative_position(violation)
        if position is None:
            unlocated.append(violation)
            continue
        key = (round(position[0] / cell_size_mm),
               round(position[1] / cell_size_mm))
        cells.setdefault(key, []).append((violation, position))

    clusters: list[Cluster] = []
    for group in cells.values():
        points = [p for _, p in group]
        center = (
            sum(x for x, _ in points) / len(points),
            sum(y for _, y in points) / len(points),
        )
        severity_counts, descriptions = _tally([v for v, _ in group])
        clusters.append(Cluster(center, len(group), severity_counts,
                                descriptions[:5]))

    if unlocated:
        severity_counts, descriptions = _tally(unlocated)
        clusters.append(Cluster(None, len(unlocated), severity_counts,
                                descriptions[:5]))

    # Biggest first, so the densest area is the first line read.
    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters


# --------------------------------------------------------------------------- #
# Optional enrichment: name the nearest footprint
# --------------------------------------------------------------------------- #
_FOOTPRINT_BOX_SCRIPT = """\
import json, sys
import pcbnew

board = pcbnew.LoadBoard(sys.argv[1])
boxes = {}
for fp in board.GetFootprints():
    box = fp.GetBoundingBox()
    boxes[fp.GetReference()] = {
        "x_min": pcbnew.ToMM(box.GetLeft()),
        "y_min": pcbnew.ToMM(box.GetTop()),
        "x_max": pcbnew.ToMM(box.GetRight()),
        "y_max": pcbnew.ToMM(box.GetBottom()),
    }
print(json.dumps(boxes, ensure_ascii=False, allow_nan=False))
"""


def nearest_footprint(center: tuple[float, float], board_path: str | None,
                      kicad_python: str | None = None) -> str | None:
    """Reference designator of the footprint nearest to ``center`` (mm, SAME
    coordinate system as the DRC report) — an OPTIONAL enrichment.

    Never raises: with no ``board_path``, or when KiCad/``pcbnew`` is not
    available on this machine, it simply returns None and the report is
    produced with the position only.

    TOOL WARNING: the real footprint list requires ``pcbnew``. This path is
    exercised in the tests with a mocked runner; it must ALSO be verified on
    a real ``.kicad_pcb`` on your machine before it is trusted.

    The helper script here reads footprint bounding boxes straight from
    pcbnew. The source module called into a project-specific collision-radar
    module for this; that module is not part of this bundle, so the box read
    is inlined instead of importing something that does not exist.
    """
    if not board_path:
        return None
    # Imported lazily: this keeps the module itself free of subprocess use for
    # every caller that only wants clustering.
    try:
        from .toolpaths import find_kicad_python, run_pcbnew_script
        find_kicad_python(kicad_python)
    except FileNotFoundError:
        return None

    try:
        with tempfile.TemporaryDirectory() as tmp:
            script_path = Path(tmp) / "_footprint_boxes.py"
            script_path.write_text(_FOOTPRINT_BOX_SCRIPT, encoding="utf-8")
            completed = run_pcbnew_script(str(script_path), [board_path],
                                          kicad_python=kicad_python,
                                          timeout_s=60)
    except Exception:                                # noqa: BLE001 - optional
        return None

    try:
        stdout = completed.stdout or ""
        boxes = json.loads(stdout.strip().splitlines()[-1]) if stdout else {}
    except (json.JSONDecodeError, IndexError):
        return None
    if not boxes:
        return None

    nearest, shortest = None, math.inf
    for ref, box in boxes.items():
        cx = (box["x_min"] + box["x_max"]) / 2
        cy = (box["y_min"] + box["y_max"]) / 2
        distance = math.dist(center, (cx, cy))
        if distance < shortest:
            nearest, shortest = ref, distance
    return nearest


def cluster_summary(cluster: Cluster, board_path: str | None = None,
                    kicad_python: str | None = None) -> str:
    """One actionable Turkish sentence per cluster:

        'Özet: {refdes veya "X,Y civarı"} etrafında {n} adet {severity}
         ihlali kümelendi. Öneri: {refdes} bölgesindeki yerleşimi genişletin.'

    Unlocated clusters get a different but still actionable sentence (the
    location/advice part is dropped, the cluster is NOT silently ignored).
    """
    dominant = (max(cluster.severity_counts, key=cluster.severity_counts.get)
                if cluster.severity_counts else "?")

    if cluster.unlocated:
        return (
            f"Özet: konum bilgisi taşımayan {cluster.count} adet {dominant} "
            "ihlali var (DRC raporu şeması bu ihlaller için 'items[].pos' "
            "içermiyor). Öneri: bu ihlalleri ayrı ayrı incelemek gerekir, "
            "bölgesel kümeleme uygulanamadı."
        )

    place = cluster.refdes
    if place is None and board_path:
        place = nearest_footprint(cluster.center, board_path, kicad_python)
        cluster.refdes = place

    x, y = cluster.center
    where = place if place else f"({x:.2f}, {y:.2f}) civarı"
    return (
        f"Özet: {where} etrafında {cluster.count} adet {dominant} ihlali "
        f"kümelendi. Öneri: "
        f"{place + ' bölgesindeki' if place else 'bu bölgedeki'} yerleşimi "
        "genişletin."
    )


def _schema_unrecognised(report: dict) -> bool:
    """A report carrying neither ``violations`` nor ``unconnected_items`` is
    an unrecognised schema — we do not know whether DRC actually ran."""
    return "violations" not in report and "unconnected_items" not in report


def drc_cluster_finding(report: dict, board_path: str | None = None,
                        kicad_python: str | None = None,
                        cell_size_mm: float = 2.0,
                        check: str = "drc_clustering") -> Finding:
    """The clustered DRC summary as a Finding.

    ``scanned`` here does NOT mean "how many violations were found" (zero
    violations can mean a genuinely clean board, which must not be reported
    as NO_COVERAGE). It answers "did the DRC engine produce a recognisable
    report at all" (is at least one of the two schema keys present).
    Unrecognised schema -> ``scanned=0`` -> automatic NO_COVERAGE: whether
    DRC really ran is UNKNOWN, and that is never silently PASS or FAIL.
    Recognised schema -> ``scanned=1``; an empty violation list is then a
    real PASS (clean board), a non-empty one is FAIL.
    """
    if _schema_unrecognised(report):
        return result(
            check, 0, [],
            "DRC raporu şeması tanınmadı (violations/unconnected_items yok) — "
            "DRC'nin gerçekten çalıştığı doğrulanamıyor.")

    violations = all_violations(report)
    clusters = cluster_violations(violations, cell_size_mm) if violations else []
    entries = [
        {
            "center": list(cluster.center) if cluster.center else None,
            "count": cluster.count,
            "severity_counts": cluster.severity_counts,
            "summary": cluster_summary(cluster, board_path, kicad_python),
        }
        for cluster in clusters
    ]
    return result(
        check, 1, entries,
        f"{len(clusters)} küme, {len(violations)} ham ihlal "
        f"(hücre boyutu {cell_size_mm}mm)"
        if violations else "0 ihlal (temiz board)")
