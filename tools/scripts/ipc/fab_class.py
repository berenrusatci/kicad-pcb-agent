#!/usr/bin/env python3
"""IPC-6012 Class 2 / Class 3 fabrication limit checker.

Checks supplied MEASUREMENTS (annular ring, solder-mask dam width, hole
aspect ratio) against the selected IPC-6012 class limits and reports through
the bundle's findings contract, so a check that received no observations is
NO_COVERAGE and never a silent PASS.

HONESTY NOTE — where the limits come from
------------------------------------------
No purchased IPC-6012 was available. Annular ring and aspect ratio come from
widely-cited Class 2/3 comparison tables. IPC-6012 itself gives no hard
solder-mask dam number, so this bundle's own accepted fab minimum
(``common.escape_geom.FAB_MIN_MASK_DAM_MM``) is used as the floor for BOTH
classes — Class 3 keeps the same floor because a LOOSER Class 3 dam would be
an error in the dangerous direction. Every limit set carries a
``source_note`` so the provenance travels with the number.

The ``limits`` constructor argument lets a caller override these
representative defaults with values they have verified against the official
table. That escape hatch is deliberate.

INPUT (positional, required) — a JSON file:
    {
      "vias":  [{"ref": "V1", "hole_mm": 0.3, "pad_mm": 0.6}, ...],
      "dams":  [{"id": "U1.1-U1.2", "width_mm": 0.22}, ...],
      "holes": [{"ref": "H1", "board_thickness_mm": 1.6, "hole_mm": 0.3}, ...]
    }
Any key may be omitted; an omitted set makes its check NO_COVERAGE.

CLI
---
    fab_class.py MEASUREMENTS_JSON [--class "Class 2"|"Class 3"]
                                   [--json OUT] [--self-test]

    exit 0  every check PASSed
    exit 1  a check FAILed, or a check had no coverage (NEEDS_HUMAN)
    exit 2  bad or missing input, or self-test failure
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.escape_geom import FAB_MIN_MASK_DAM_MM  # noqa: E402
from common.findings import Finding, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.ipc.report import CHECK_SCHEMA, check_exit_code, check_report  # noqa: E402

SCHEMA_VERSION = CHECK_SCHEMA
TOOL_NAME = "fab_class"
STANDARD = "IPC-6012"

LIMITS = (
    "Annular ring and aspect-ratio limits are representative secondary-source "
    "values; verify each against an official IPC-6012 table.",
    "IPC-6012 gives no hard solder-mask dam number. The floor used here is "
    "this bundle's own fab minimum (common.escape_geom.FAB_MIN_MASK_DAM_MM), "
    "not an IPC-6012 requirement.",
    "Measurements are supplied by the caller; this tool does not read a "
    "board file and cannot verify that the measurement set is complete.",
)


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı. Girdi
# `_build_parser()` argparse bloğundan, çıktı `scripts.ipc.report.check_report()`
# (tools/scripts/ipc/report.py s.73-83) + schemas/ipc_check_report.schema.json'dan
# türetildi. Şemanın `required` listesi ile bu sınıfın alanları BİREBİR aynıdır.
# --------------------------------------------------------------------------- #
class FabClassInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `_build_parser()` (s.357-370).

    `fab_class` CLI'da `--class` adıyla geçer (`class` Python'da anahtar sözcük
    olduğu için `dest="fab_class"`); seçenekler `FabClass` enum'unun
    değerleridir ve varsayılan "Class 2"dir. Sınıfı YÜKSELTMEK sınırları
    SIKILAŞTIRIR (Class 3: daha büyük halka, daha düşük aspect ratio).
    """

    fab_class: str      # --class: "Class 2" | "Class 3", vars. "Class 2"
    json: str           # --json: JSON raporunun da yazılacağı yol
    self_test: bool     # --self-test: kusur enjeksiyonu dahil öz-testleri koşar, çıkar


class FabClassInput(FabClassInputOptional):
    """Aracın tam girdisi. Kaynak: `_build_parser()` (s.357-370).

    `measurements` argparse'ta `nargs="?"` ile tanımlıdır ama `--self-test`
    yoksa `parser.error()` ile ZORUNLU kılınır (s.386-388): `measurements` ya da
    `self_test` — biri şart. Dosyanın içeriği dosya başındaki INPUT bloğudur:
    `vias` [{ref, hole_mm, pad_mm}], `dams` [{id, width_mm}], `holes`
    [{ref, board_thickness_mm, hole_mm}]. Herhangi biri ATLANABİLİR — ama
    atlanan küme o kontrolü NO_COVERAGE yapar, sessiz PASS'a değil.
    """

    measurements: str   # konumsal: ölçüm JSON dosyasının yolu


class FabClassOutput(TypedDict):
    """`--json` ile yazılan raporun üst düzey anahtarları. Kaynak:
    `scripts.ipc.report.check_report()` (s.73-83) + schemas/
    ipc_check_report.schema.json (`required` = bu dokuz alanın hepsi).

    `verdict` BİLEREK üç değerlidir: ölçüm almamış bir kontrol PASS değil
    NEEDS_HUMAN'dır. Bu araç kart dosyası OKUMAZ; ölçümler çağırandan gelir, o
    yüzden "ölçüm kümesi eksik mi" sorusunu araç yanıtlayamaz (bkz. `limits`).

    ÇIKIŞ KODLARI (dosya başı notu): 0 = her kontrol PASS, 1 = bir kontrol FAIL
    ya da kapsamsız (NEEDS_HUMAN), 2 = bozuk/eksik girdi ya da self-test
    başarısız. `check_exit_code()` kart denetleyicilerinden DAHA SIKIDIR: orada
    NO_COVERAGE 0 döner, burada 1.
    """

    schema_version: str  # CHECK_SCHEMA (scripts/ipc/report.py'den)
    tool: str            # "fab_class"
    standard: str        # "IPC-6012"
    subject: str         # ölçüm JSON dosyasının yolu
    params: dict         # {"class", "min_annular_ring_mm", "min_mask_dam_mm", "max_aspect_ratio", "source_note"}
    checks: list         # Finding listesi: ipc6012_annular_ring / _solder_mask_dam / _aspect_ratio
    summary: dict        # {"PASS": n, "FAIL": n, "NO_COVERAGE": n}
    verdict: str         # PASS | FAIL | NEEDS_HUMAN (kapsamsız kontrol PASS DEĞİLDİR)
    limits: list         # modül düzeyi LIMITS: sayıların ikincil kaynaklı olduğu uyarısı


class FabClass(str, Enum):
    CLASS_2 = "Class 2"
    CLASS_3 = "Class 3"


@dataclass(frozen=True)
class ClassLimits:
    """The three limits this module checks for one IPC-6012 class.

    ``source_note`` is REQUIRED on every limit set so that reports can always
    say which secondary source or assumption a number came from.
    """

    fab_class: FabClass
    min_annular_ring_mm: float
    min_mask_dam_mm: float
    max_aspect_ratio: float
    source_note: str


CLASS_LIMITS: dict[FabClass, ClassLimits] = {
    FabClass.CLASS_2: ClassLimits(
        fab_class=FabClass.CLASS_2,
        min_annular_ring_mm=0.05,
        min_mask_dam_mm=FAB_MIN_MASK_DAM_MM,
        max_aspect_ratio=10.0,
        source_note=(
            "REPRESENTATIVE (secondary source) — annular ring / aspect ratio "
            "must be verified one by one against the official IPC-6012 table; "
            "the mask dam floor is inherited from "
            "common.escape_geom.FAB_MIN_MASK_DAM_MM."),
    ),
    FabClass.CLASS_3: ClassLimits(
        fab_class=FabClass.CLASS_3,
        min_annular_ring_mm=0.075,
        min_mask_dam_mm=FAB_MIN_MASK_DAM_MM,
        max_aspect_ratio=8.0,
        source_note=(
            "REPRESENTATIVE (secondary source) — Class 3 (high reliability: "
            "aerospace/medical/military) demands STRICTER limits than Class 2 "
            "(larger annular ring, lower aspect ratio); verify one by one "
            "against the official IPC-6012 table."),
    ),
}


@dataclass(frozen=True)
class ViaMeasurement:
    """One measured via. ``hole_mm`` is the nominal drill diameter BEFORE
    plating; ``pad_mm`` is the outer copper pad diameter."""

    ref: str
    hole_mm: float
    pad_mm: float

    @property
    def annular_ring_mm(self) -> float:
        """(pad - hole) / 2 — the single-sided ring width, which is the
        definition used in fabrication."""
        return (self.pad_mm - self.hole_mm) / 2.0


@dataclass(frozen=True)
class DamMeasurement:
    """One solder-mask dam (the bridge between two openings)."""

    id: str
    width_mm: float


@dataclass(frozen=True)
class HoleMeasurement:
    """One hole, for the aspect ratio (board thickness / hole diameter)."""

    ref: str
    board_thickness_mm: float
    hole_mm: float

    @property
    def aspect_ratio(self) -> float:
        if self.hole_mm <= 0:
            raise ValueError(f"{self.ref}: hole_mm must be positive")
        return self.board_thickness_mm / self.hole_mm


class FabClassEngine:
    """Check measurement sets against one IPC-6012 class."""

    def __init__(self, fab_class: FabClass,
                 limits: ClassLimits | None = None) -> None:
        self.fab_class = fab_class
        # The `limits` override lets a user replace these representative
        # defaults with values they have verified against the official
        # table. Deliberate escape hatch, not an oversight.
        self.limits = limits if limits is not None else CLASS_LIMITS[fab_class]

    def check_annular_ring(self, vias: Sequence[ViaMeasurement]) -> Finding:
        violations = [
            {
                "ref": v.ref,
                "measured_mm": round(v.annular_ring_mm, 4),
                "minimum_mm": self.limits.min_annular_ring_mm,
                "short_by_mm": round(
                    self.limits.min_annular_ring_mm - v.annular_ring_mm, 4),
            }
            for v in vias
            if v.annular_ring_mm < self.limits.min_annular_ring_mm
        ]
        return result(
            "ipc6012_annular_ring", len(vias), violations,
            f"{self.fab_class.value}, min={self.limits.min_annular_ring_mm} mm "
            f"({self.limits.source_note})")

    def check_mask_dam(self, dams: Sequence[DamMeasurement]) -> Finding:
        violations = [
            {
                "id": d.id,
                "measured_mm": d.width_mm,
                "minimum_mm": self.limits.min_mask_dam_mm,
            }
            for d in dams
            if d.width_mm < self.limits.min_mask_dam_mm
        ]
        return result(
            "ipc6012_solder_mask_dam", len(dams), violations,
            f"{self.fab_class.value}, min={self.limits.min_mask_dam_mm} mm "
            "(source: common.escape_geom.FAB_MIN_MASK_DAM_MM, NOT IPC-6012 itself)")

    def check_aspect_ratio(self, holes: Sequence[HoleMeasurement]) -> Finding:
        violations = [
            {
                "ref": h.ref,
                "measured_ratio": round(h.aspect_ratio, 3),
                "maximum_ratio": self.limits.max_aspect_ratio,
                "board_thickness_mm": h.board_thickness_mm,
                "hole_mm": h.hole_mm,
            }
            for h in holes
            if h.aspect_ratio > self.limits.max_aspect_ratio
        ]
        return result(
            "ipc6012_aspect_ratio", len(holes), violations,
            f"{self.fab_class.value}, max={self.limits.max_aspect_ratio}:1 "
            f"({self.limits.source_note})")

    def run_all(self, vias: Sequence[ViaMeasurement] = (),
                dams: Sequence[DamMeasurement] = (),
                holes: Sequence[HoleMeasurement] = ()) -> list[Finding]:
        """Run all three checks. An empty measurement set makes its check
        NO_COVERAGE — it is never silently counted as a PASS."""
        return [
            self.check_annular_ring(vias),
            self.check_mask_dam(dams),
            self.check_aspect_ratio(holes),
        ]


def measurements_from_json(data: dict) -> tuple[
        list[ViaMeasurement], list[DamMeasurement], list[HoleMeasurement]]:
    """Parse the input document. Missing keys yield empty sets (=> NO_COVERAGE).

    Kept independent of any board API on purpose: the caller converts from
    pcbnew (``pad.GetDrillSize()`` / ``pad.GetSize()``) or from exported
    Gerbers into this simple intermediate shape.
    """
    if not isinstance(data, dict):
        raise ValueError("measurements file must contain a JSON object")

    vias = [ViaMeasurement(str(r.get("ref", "?")),
                           float(r["hole_mm"]), float(r["pad_mm"]))
            for r in data.get("vias", [])]
    dams = [DamMeasurement(str(r.get("id", "?")), float(r["width_mm"]))
            for r in data.get("dams", [])]
    holes = [HoleMeasurement(str(r.get("ref", "?")),
                             float(r["board_thickness_mm"]), float(r["hole_mm"]))
             for r in data.get("holes", [])]
    return vias, dams, holes


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: a via whose ring is deliberately just below the
    Class 2 minimum must FAIL. If it passes, the comparison is empty."""
    limits = CLASS_LIMITS[FabClass.CLASS_2]
    hole = 0.30
    pad = hole + 2 * (limits.min_annular_ring_mm - 0.01)
    finding = FabClassEngine(FabClass.CLASS_2).check_annular_ring(
        [ViaMeasurement("V_FAULT", hole, pad)])
    return finding["status"] == "FAIL"


def run_self_tests() -> list[str]:
    failures: list[str] = []

    # Class 3 must be strictly tighter than Class 2 on both real limits.
    c2, c3 = CLASS_LIMITS[FabClass.CLASS_2], CLASS_LIMITS[FabClass.CLASS_3]
    if not c3.min_annular_ring_mm > c2.min_annular_ring_mm:
        failures.append("Class 3 annular ring is not stricter than Class 2")
    if not c3.max_aspect_ratio < c2.max_aspect_ratio:
        failures.append("Class 3 aspect ratio is not stricter than Class 2")

    # Empty input must be NO_COVERAGE, never PASS.
    for finding in FabClassEngine(FabClass.CLASS_2).run_all():
        if finding["status"] != "NO_COVERAGE":
            failures.append(
                f"{finding['check']}: empty input produced "
                f"{finding['status']}, expected NO_COVERAGE")

    # A comfortably compliant via must PASS.
    good = FabClassEngine(FabClass.CLASS_2).check_annular_ring(
        [ViaMeasurement("V_OK", 0.30, 0.30 + 2 * 0.20)])
    if good["status"] != "PASS":
        failures.append(f"a compliant via reported {good['status']}")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: the annular-ring comparison may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("measurements", type=Path, nargs="?",
                        help="JSON file of measurements "
                             "(required unless --self-test)")
    parser.add_argument("--class", dest="fab_class",
                        choices=[e.value for e in FabClass],
                        default=FabClass.CLASS_2.value)
    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        failures = run_self_tests()
        for failure in failures:
            diag(f"SELF-TEST FAIL: {failure}")
        if failures:
            return 2
        diag("SELF-TEST PASS")
        return 0

    if args.measurements is None:
        parser.error("a measurements JSON file is required; pass it "
                     "positionally or use --self-test")

    data = json.loads(Path(args.measurements).read_text(encoding="utf-8"))
    vias, dams, holes = measurements_from_json(data)

    fab_class = FabClass(args.fab_class)
    engine = FabClassEngine(fab_class)
    findings = engine.run_all(vias, dams, holes)

    report = check_report(
        TOOL_NAME, STANDARD, str(args.measurements),
        params={
            "class": fab_class.value,
            "min_annular_ring_mm": engine.limits.min_annular_ring_mm,
            "min_mask_dam_mm": engine.limits.min_mask_dam_mm,
            "max_aspect_ratio": engine.limits.max_aspect_ratio,
            "source_note": engine.limits.source_note,
        },
        findings=findings,
        limits=LIMITS,
    )
    write_report(report, args.json)
    emit(report)
    return check_exit_code(report)


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
