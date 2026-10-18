#!/usr/bin/env python3
"""Post-layout parasitic extraction CLI (#64).

backend: cli  (network: none; reads the .kicad_pcb text directly)

Reads routed geometry out of a ``.kicad_pcb`` WITHOUT pcbnew - see
``common/parasitics.py`` for why the JSON/IPC bridge was measured to be
unnecessary - solves each distinct cross-section numerically, and writes
per-net R/L/C plus an includable SPICE library for back-annotation.

Usage:
    parasitics.py board.kicad_pcb --stackup stack.json [--net NAME ...]
                  [--spice out.cir] [--json report.json]
    parasitics.py --self-test

The stackup file is required and every layer must carry a ``source``: an er
and a dielectric height multiply every number downstream, so an uncited one
makes the whole extraction uncheckable. There is deliberately no ``--er``
flag.

Exit: 0 PASS, 1 FAIL/NO_COVERAGE, 2 could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import Finding, result, summarise  # noqa: E402
from common.hashing import sha256_file  # noqa: E402
from common.parasitics import (  # noqa: E402
    COPPER_RHO_OHM_M,
    COPPER_RHO_SOURCE,
    MODEL_CLASS,
    SIGNOFF,
    LayerSpec,
    ParasiticError,
    count_vias,
    extract,
    read_segments,
    to_spice_library,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "parasitics-report/v1"
TOOL_NAME = "parasitics"

LIMITS = [
    "DC resistance only - no skin effect, so R is optimistic at speed",
    "no coupling BETWEEN segments: each is solved alone over its plane, so "
    "crosstalk is absent by construction",
    "vias are counted, not modelled - via inductance is missing",
    "lumped single pi section per net; above the reported validity frequency "
    "the net is a transmission line and this model is the wrong shape",
    "quasi-static, lossless dielectric: no tan(delta), no dispersion",
]


def load_stackup(path: str) -> dict[str, LayerSpec]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    layers = payload.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise ParasiticError(f"{path}: 'layers' must be a non-empty object")
    out: dict[str, LayerSpec] = {}
    for name, raw in layers.items():
        missing = [k for k in ("er", "height_mm", "copper_thickness_mm",
                               "source") if k not in raw]
        if missing:
            raise ParasiticError(
                f"{path}: layer '{name}' is missing {missing}. 'source' is "
                "not optional - see the module docstring.")
        out[name] = LayerSpec(name=name, er=float(raw["er"]),
                              height_mm=float(raw["height_mm"]),
                              copper_thickness_mm=float(
                                  raw["copper_thickness_mm"]),
                              source=str(raw["source"]))
    return out


def build_report(board: Path, stackup_path: str, extracted: dict,
                 segments, vias: int, spice_path: str | None) -> dict:
    nets = extracted["nets"]
    findings = []

    if not nets:
        findings.append(Finding(
            "parasitics_extracted", "NO_COVERAGE", 0, [],
            "no net produced parasitics: every routed segment sat on a layer "
            "the stackup does not describe, or --net matched nothing. A "
            "layer nobody described is not a layer with default values."))
    else:
        findings.append(result(
            "parasitics_extracted", len(nets), [],
            f"{len(nets)} net(s) from {len(segments)} routed segment(s)"))

    skipped = extracted["skipped_layers"]
    findings.append(result(
        "stackup_covers_routed_layers",
        len({s.layer for s in segments}),
        [f"{layer}: {count} segment(s) skipped - not in the stackup"
         for layer, count in sorted(skipped.items())],
        "every layer carrying copper must be described; an undescribed one "
        "is silently missing parasitics, not zero parasitics"))

    findings.append(Finding(
        "vias_modelled", "NO_COVERAGE", 0, [],
        f"{vias} via(s) on this board are COUNTED and NOT MODELLED. Via "
        "inductance is absent from every number in this report; a net whose "
        "path is mostly vias is under-reported here."))

    rows = []
    for name, entry in sorted(nets.items()):
        rows.append({
            "net": name, "segments": entry.segments,
            "length_mm": round(entry.length_mm, 4),
            "layers": sorted(entry.layers),
            "r_ohm": round(entry.r_ohm, 6),
            "l_nh": round(entry.l_nh, 6),
            "c_pf": round(entry.c_pf, 6),
            "longest_segment_mm": round(entry.longest_segment_mm, 4),
            "lumped_valid_below_hz": entry.lumped_valid_to_hz(
                extracted["cross_sections"] and next(
                    iter(extracted["cross_sections"].values()))["er_eff"] or 1.0),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "design": {"path": str(board), "sha256": sha256_file(board)},
        "inputs": {"stackup": stackup_path,
                   "rho_ohm_m": COPPER_RHO_OHM_M,
                   "rho_source": COPPER_RHO_SOURCE},
        "model_class": MODEL_CLASS,
        "signoff": SIGNOFF,
        "limits": LIMITS,
        "cross_sections": extracted["cross_sections"],
        "nets": rows,
        "spice_library": spice_path,
        "checks": findings,
        "summary": summarise(findings),
    }


def _self_test() -> int:
    board = ("(kicad_pcb (version 20240108)\n"
             '(segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") '
             '(net "SIG"))\n'
             '(segment (start 10 0) (end 20 0) (width 0.2) (layer "F.Cu") '
             '(net "SIG"))\n'
             '(segment (start 0 5) (end 10 5) (width 0.2) (layer "In1.Cu") '
             '(net "SIG"))\n)')
    segments = read_segments(board)
    assert len(segments) == 3, segments
    assert abs(sum(s.length_mm for s in segments) - 30.0) < 1e-9

    layers = {"F.Cu": LayerSpec("F.Cu", 4.3, 0.1, 0.035, "self-test fixture")}
    out = extract(segments, layers, cells_per_feature=2, max_nodes=30_000)
    entry = out["nets"]["SIG"]
    assert entry.segments == 2, "the In1.Cu segment must NOT be counted"
    assert out["skipped_layers"] == {"In1.Cu": 1}, out["skipped_layers"]

    # R against hand arithmetic: rho * L / (w * t), 20 mm of 0.2x0.035 mm.
    expected_r = COPPER_RHO_OHM_M * 0.020 / (0.2e-3 * 0.035e-3)
    assert abs(entry.r_ohm - expected_r) / expected_r < 1e-9, (
        f"R={entry.r_ohm} expected {expected_r}")

    # FAULT INJECTION: an uncited stackup layer must be refused outright.
    try:
        LayerSpec("F.Cu", 4.3, 0.1, 0.035, "   ")
    except ParasiticError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a stackup layer without a source was accepted")

    try:
        extract([], layers)
    except ParasiticError:
        pass
    else:  # pragma: no cover
        raise AssertionError("an unrouted board must not extract silently")

    diag("SELF-TEST PASS: geometry read without pcbnew, R matches closed "
         "arithmetic, undescribed layers are skipped not guessed, and both "
         "refusals fire")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("board", nargs="?")
    ap.add_argument("--stackup")
    ap.add_argument("--net", action="append", default=[])
    ap.add_argument("--spice", help="write the SPICE library here")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--cells-per-feature", type=int, default=3,
                    dest="cells_per_feature")
    ap.add_argument("--max-nodes", type=int, default=120_000, dest="max_nodes")
    ap.add_argument("--self-test", action="store_true", dest="self_test")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.board:
        ap.error("give a board, or --self-test")
    if not a.stackup:
        ap.error("--stackup is required: an er and a height without a source "
                 "make the extraction uncheckable")

    board = Path(a.board).expanduser().resolve()
    if not board.is_file():
        raise ParasiticError(f"board not found: {board}")
    text = board.read_text(encoding="utf-8", errors="replace")

    segments = read_segments(text)
    layers = load_stackup(a.stackup)
    extracted = extract(segments, layers, nets=a.net or None,
                        cells_per_feature=a.cells_per_feature,
                        max_nodes=a.max_nodes)

    spice_path = None
    if a.spice and extracted["nets"]:
        Path(a.spice).write_text(to_spice_library(extracted), encoding="utf-8")
        spice_path = a.spice

    report = build_report(board, a.stackup, extracted, segments,
                          count_vias(text), spice_path)
    write_report(report, a.json_path)
    emit(report)
    diag(f"parasitics: {len(report['nets'])} net(s), "
         f"{report['summary']['FAIL']} FAIL, "
         f"{report['summary']['NO_COVERAGE']} NO_COVERAGE")
    # The docstring's Exit contract says "1 FAIL/NO_COVERAGE" and the code
    # said FAIL only, so every run left through exit 0 while carrying the
    # permanent `vias_modelled` NO_COVERAGE. The contract is the correct one
    # of the two: this is a SCREEN-class tool with signoff=False, and while
    # via inductance is counted but not modelled its report can never be a
    # pass. It therefore exits non-zero on every run BY DESIGN - that is the
    # tool telling the truth about itself, not a bug to be tuned away.
    return 1 if (report["summary"]["FAIL"]
                 or report["summary"]["NO_COVERAGE"]
                 or not extracted["nets"]) else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
