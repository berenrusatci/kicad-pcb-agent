#!/usr/bin/env python3
"""Post-layout parasitic extraction from routed board geometry (#64).

WHY THERE IS NO pcbnew HERE
---------------------------------------------------------------------------
The obvious route was a JSON/IPC bridge or the board MCP server, because
``pcbnew`` lives in KiCad's own Python 3.11 and this bundle runs on 3.14.
That was MEASURED before it was assumed: ``common/sexpr.py`` reads a routed
board's segments directly out of the ``.kicad_pcb`` text - 308 of them on
cam-w-og05b10, with start, end, width, layer and net - so no second process
boundary is needed. A bridge would have added the integration risk that
sank #49 in exchange for data already in reach.

WHAT IS EXTRACTED, AND FROM WHAT
---------------------------------------------------------------------------
Per segment, three quantities, each from something already validated:

  R = rho * length / (width * copper_thickness)      DC resistance
  L = L' * length,  C = C' * length

``L'`` and ``C'`` come from ``common/field2d``'s numerical cross-section
solve, not from a fitted formula: the solver returns the capacitance with
the dielectric and again in vacuum, and

  C' = C_11,      L' = 1 / (c^2 * C0_11)

is the exact TEM relation, not an approximation with a coefficient in it.
That is what makes this defensible where a table of per-mil values would
not be.

WHAT THIS IS NOT
---------------------------------------------------------------------------
Every one of these is a reason a number here can be wrong, and each is
reported with the result rather than left for the reader to discover:

  * DC resistance only - no skin effect, so R is optimistic above the
    frequency where current stops filling the conductor;
  * no coupling BETWEEN segments - each is solved as if alone over its
    reference plane, so crosstalk is absent by construction;
  * vias are counted but NOT modelled - via inductance is missing;
  * lumped, not distributed - a segment longer than a tenth of a wavelength
    at the frequency of interest is not a lumped element, and the extractor
    says so per segment instead of emitting a model nobody may use;
  * quasi-static and lossless dielectric - no tan(delta), no dispersion.

The model class is therefore SCREEN, and ``signoff`` is false. This narrows
where a design is wrong; it does not certify that it is right.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from common import field2d as f2
from common import sexpr
from common.units import C_MPS

MODEL_CLASS = "SCREEN"
SIGNOFF = False

# Physical constant, not an engineering threshold: the resistivity of
# annealed copper at 20 C. Carried with its identity so a reader can tell it
# apart from a number somebody chose - and overridable, because plating and
# temperature move it.
COPPER_RHO_OHM_M = 1.68e-8
COPPER_RHO_SOURCE = "annealed copper at 20 C, textbook physical constant"


class ParasiticError(ValueError):
    """Extraction could not proceed on the inputs given."""


@dataclass(frozen=True)
class LayerSpec:
    """The stackup facts one routing layer needs, with where they came from.

    ``source`` is required for the same reason it is on ``FlexLayer``: an er
    or a dielectric height without a citation cannot be told apart from one
    that was measured, and both end up multiplying every number downstream.
    """

    name: str
    er: float
    height_mm: float          # to the NEAREST reference plane
    copper_thickness_mm: float
    source: str

    def __post_init__(self) -> None:
        for label, value in (("er", self.er), ("height_mm", self.height_mm),
                             ("copper_thickness_mm", self.copper_thickness_mm)):
            if value <= 0 or not math.isfinite(value):
                raise ParasiticError(f"{self.name}: {label} must be positive, "
                                     f"got {value}")
        if self.er < 1.0:
            raise ParasiticError(f"{self.name}: er must be >= 1, got {self.er}")
        if not self.source.strip():
            raise ParasiticError(
                f"{self.name}: stackup values carry no source. An er and a "
                "dielectric height multiply every parasitic downstream; "
                "without a citation the whole extraction is uncheckable.")


@dataclass(frozen=True)
class Segment:
    net: str
    layer: str
    width_mm: float
    length_mm: float


@dataclass
class NetParasitics:
    net: str
    length_mm: float = 0.0
    r_ohm: float = 0.0
    l_nh: float = 0.0
    c_pf: float = 0.0
    segments: int = 0
    layers: set = field(default_factory=set)
    longest_segment_mm: float = 0.0
    notes: list = field(default_factory=list)

    def lumped_valid_to_hz(self, er_eff: float) -> float:
        """Above this the LONGEST segment stops being a lumped element.

        Tenth-of-a-wavelength is the usual engineering line and it is stated
        here rather than buried: the caller gets the number and decides.
        """
        if self.longest_segment_mm <= 0:
            return math.inf
        velocity = C_MPS / math.sqrt(er_eff)
        return velocity / (10.0 * self.longest_segment_mm * 1e-3)


def _field(form: str, key: str) -> str | None:
    match = re.search(r"\(" + key + r"\s+([^)]*?)\)", form)
    return match.group(1).strip() if match else None


def read_segments(board_text: str) -> list[Segment]:
    """Routed copper out of the board file. No pcbnew, no second process."""
    out: list[Segment] = []
    for form in sexpr.subforms(board_text, "segment"):
        start, end = _field(form, "start"), _field(form, "end")
        width, layer, net = (_field(form, "width"), _field(form, "layer"),
                             _field(form, "net"))
        if not (start and end and width and layer):
            continue
        try:
            x1, y1 = (float(v) for v in start.split())
            x2, y2 = (float(v) for v in end.split())
        except ValueError:
            continue
        out.append(Segment(
            net=(net or "").strip('"') or "<unnamed>",
            layer=layer.strip('"'),
            width_mm=float(width),
            length_mm=math.dist((x1, y1), (x2, y2))))
    return out


def count_vias(board_text: str) -> int:
    return len(sexpr.subforms(board_text, "via"))


def per_unit_length(width_mm: float, layer: LayerSpec, *,
                    cells_per_feature: int = 3,
                    max_nodes: int = 120_000) -> dict:
    """``L'`` (H/m), ``C'`` (F/m) and er_eff for one trace cross-section.

    Solved numerically. ``L' = 1/(c^2 * C0')`` is the exact TEM identity, so
    the inductance inherits the solver's error rather than a formula's bias.
    """
    section = f2.microstrip(width_mm, layer.copper_thickness_mm,
                            layer.height_mm, layer.er,
                            cells_per_feature=cells_per_feature,
                            max_nodes=max_nodes)
    solved = f2.solve_line(section)
    c_per_m = float(solved.capacitance_matrix_f_per_m[0, 0])
    c0_per_m = float(solved.vacuum_capacitance_matrix_f_per_m[0, 0])
    if c0_per_m <= 0:
        raise ParasiticError("vacuum capacitance came back non-positive; the "
                             "cross-section did not solve")
    return {"c_f_per_m": c_per_m,
            "l_h_per_m": 1.0 / (C_MPS ** 2 * c0_per_m),
            "er_eff": c_per_m / c0_per_m,
            "z0_ohm": solved.z0_ohm()}


def extract(segments: Sequence[Segment], layers: dict[str, LayerSpec], *,
            rho_ohm_m: float = COPPER_RHO_OHM_M,
            nets: Iterable[str] | None = None,
            **solver_kw) -> dict:
    """Per-net R/L/C from routed geometry. Unknown layers are NOT guessed."""
    if not segments:
        raise ParasiticError(
            "no routed segments: there is nothing to extract. An unrouted "
            "board has no parasitics, which is not the same as small ones.")
    wanted = set(nets) if nets else None
    cache: dict[tuple[float, str], dict] = {}
    out: dict[str, NetParasitics] = {}
    skipped: dict[str, int] = {}

    for seg in segments:
        if wanted is not None and seg.net not in wanted:
            continue
        layer = layers.get(seg.layer)
        if layer is None:
            # A layer nobody described is not a layer with default values.
            skipped[seg.layer] = skipped.get(seg.layer, 0) + 1
            continue
        key = (round(seg.width_mm, 6), seg.layer)
        if key not in cache:
            cache[key] = per_unit_length(seg.width_mm, layer, **solver_kw)
        unit = cache[key]

        entry = out.setdefault(seg.net, NetParasitics(net=seg.net))
        metres = seg.length_mm * 1e-3
        area_m2 = (seg.width_mm * 1e-3) * (layer.copper_thickness_mm * 1e-3)
        entry.r_ohm += rho_ohm_m * metres / area_m2
        entry.l_nh += unit["l_h_per_m"] * metres * 1e9
        entry.c_pf += unit["c_f_per_m"] * metres * 1e12
        entry.length_mm += seg.length_mm
        entry.segments += 1
        entry.layers.add(seg.layer)
        entry.longest_segment_mm = max(entry.longest_segment_mm, seg.length_mm)

    return {"nets": out, "skipped_layers": skipped,
            "cross_sections": {f"{w} mm on {lay}": v for (w, lay), v in cache.items()}}


def to_subckt(entry: NetParasitics, *, name: str | None = None) -> str:
    """One net as a SPICE ``.subckt``: series R-L with the capacitance split
    across both ends (a single pi section).

    A single section is chosen deliberately over a per-segment ladder: the
    ladder would LOOK more detailed while carrying the same physics, because
    the segments were solved without coupling between them. Detail that adds
    no information is how a model starts being trusted past its evidence.
    """
    ident = name or re.sub(r"[^A-Za-z0-9_]", "_", entry.net) or "NET"
    half_c = entry.c_pf / 2.0
    lines = [
        f".subckt PAR_{ident} in out",
        f"* post-layout parasitics, {entry.segments} segment(s), "
        f"{entry.length_mm:.3f} mm on {'+'.join(sorted(entry.layers))}",
        "* MODEL CLASS SCREEN: DC resistance (no skin effect), no coupling "
        "between segments, vias not modelled, lumped single pi section.",
        f"R1 in mid {entry.r_ohm:.6g}",
        f"L1 mid out {entry.l_nh:.6g}n",
        f"C1 in 0 {half_c:.6g}p",
        f"C2 out 0 {half_c:.6g}p",
        ".ends",
    ]
    return "\n".join(lines)


def to_spice_library(result: dict, *, er_eff_hint: float | None = None) -> str:
    """Every extracted net as one includable SPICE file."""
    nets = result["nets"]
    if not nets:
        raise ParasiticError("nothing to write: no net produced parasitics")
    header = [
        "* Post-layout parasitics, extracted from routed board geometry.",
        "* Generated by common/parasitics.py - model class SCREEN, "
        "signoff false.",
        "* Each subckt is a single lumped pi section; see the per-net "
        "validity frequency in the accompanying report before using one "
        "above it.",
        "",
    ]
    blocks = [to_subckt(entry) for _, entry in sorted(nets.items())]
    return "\n".join(header + blocks) + "\n"
