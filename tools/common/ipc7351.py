"""IPC-7351 SMD land-pattern (footprint pad) calculator.

Ported from ``ipc7351_footprint.py``.

WHY THIS EXISTS: footprint/pad sizing used to be left to a human ("copy the
land pattern out of the datasheet") — a correct default, but for standard
two-terminal chip passives (0402/0603/0805 R/C/L) a land pattern can be
CALCULATED and CHECKED with the IPC-7351B formula. This module does that
calculation.

FORMULAS (IPC-7351B, two-terminal chip component — chip R/C/L):
    Zmax (outer span)   = Lmin + 2*Jt + sqrt(Cl^2 + F^2 + P^2)
    Gmin (inner gap)    = Smax - 2*Jh - sqrt(Cs^2 + F^2 + P^2)
    Xmax (pad width)    = Wmin + 2*Js + sqrt(Cs^2 + F^2 + P^2)

    where S = L - 2*T (heel-to-heel distance between terminations)
    Jt/Jh/Js = toe/heel/side fillet targets (by density level)
    Cl/Cs = component tolerance, F = fabrication tolerance,
    P = placement tolerance

Pad dimensions: pad_length = (Zmax - Gmin) / 2, pad_width = Xmax.

HONESTY NOTE (kept from the source, do not delete):
The default Jt/Jh/Js/F/P/Cl/Cs values here are the commonly PUBLISHED
IPC-7351B defaults (referenced to Density Level B / Nominal). They are NOT
sufficient for SIGN-OFF: on a critical or dense design they must be
CROSS-CHECKED against the real IPC-7351B table (or a dedicated IPC-7351
calculator such as PCB Libraries' own tool), especially for unusual
packages. The purpose of this module is "a reasonable starting point plus a
traceable calculation", not an unquestioned automatic source of library
parts.

TUR 3 ADDITION — gullwing/QFN/BGA were the module's own documented "TBD" gap;
the source's later drift filled exactly that gap and is ported here:

  * ``gullwing_land_pattern`` (SOIC/TSOP/QFP) and ``qfn_land_pattern``
    (no-lead) reuse the SAME Zmax/Gmin/Xmax toe/heel/side formula as
    ``land_pattern`` above — only the (L, W, T, fillet table) inputs differ.
    The shared arithmetic is now factored into ``_land_pattern_core`` so the
    three callers cannot drift apart from each other (the source's own stated
    reason for doing the same).
  * ``bga_land_pattern`` is a COMPLETELY DIFFERENT, simpler formula family —
    IPC-7351B sizes a BGA pad as a ratio of the ball diameter, not from a
    toe/heel/side fillet budget. See its own docstring for the honesty note
    on the 0.80/1.00 multipliers.

Library only: no CLI, no stdout writes. stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class DensityLevel(str, Enum):
    """The three IPC-7351B density levels — the same package yields three
    different pad sizes. The choice follows the manufacturing/reliability
    target; it is not arbitrary."""

    A_MAXIMUM = "A"   # Widest pad (easiest soldering, most area)
    B_NOMINAL = "B"   # The default for most designs
    C_MINIMUM = "C"   # Densest packing (fine pitch, area constrained)


# Fillet targets (mm) per density level — the commonly published defaults of
# the IPC-7351B chip/SMD component table.
_FILLET_MM = {
    DensityLevel.A_MAXIMUM: {"Jt": 0.55, "Jh": 0.00, "Js": 0.05},
    DensityLevel.B_NOMINAL: {"Jt": 0.35, "Jh": 0.00, "Js": 0.03},
    DensityLevel.C_MINIMUM: {"Jt": 0.15, "Jh": 0.00, "Js": 0.01},
}

# Default tolerance budget (mm) — F: fabrication, P: placement, Cl/Cs:
# component tolerance. On a critical design these must be UPDATED from the
# datasheet / fab profile; these are typical SMT production defaults.
DEFAULT_F_MM = 0.05
DEFAULT_P_MM = 0.05
DEFAULT_CL_MM = 0.10
DEFAULT_CS_MM = 0.10


@dataclass
class ChipComponent:
    """RAW dimensions (mm) taken from the datasheet. Without explicit
    min/max tolerances, a symmetric band of ``tolerance_mm`` is assumed."""

    length_nom_mm: float        # L: body length, end to end, terminations included
    width_nom_mm: float         # W: body width
    termination_nom_mm: float   # T: termination (end metal) length
    tolerance_mm: float = 0.05  # symmetric ± tolerance for L/W/T


@dataclass
class LandPattern:
    pad_length_mm: float
    pad_width_mm: float
    pad_pitch_mm: float   # centre to centre distance of the two pads
    zmax_mm: float
    gmin_mm: float
    xmax_mm: float
    density: DensityLevel


def _land_pattern_core(
    l_nom_mm: float, w_nom_mm: float, t_nom_mm: float, tolerance_mm: float,
    fillet: dict, f_mm: float, p_mm: float, cl_mm: float, cs_mm: float,
    density: DensityLevel,
) -> LandPattern:
    """The SHARED core of Zmax/Gmin/Xmax — ``land_pattern`` (two-terminal
    chip), ``gullwing_land_pattern`` (SOIC/QFP) and ``qfn_land_pattern``
    (no-lead) all use the same IPC-7351B toe/heel/side formula family; only
    the (L, W, T, fillet table) input differs. Kept in one place so the three
    callers cannot drift apart from each other.
    """
    l_min = l_nom_mm - tolerance_mm
    l_max = l_nom_mm + tolerance_mm
    w_min = w_nom_mm - tolerance_mm

    root_long = math.sqrt(cl_mm ** 2 + f_mm ** 2 + p_mm ** 2)
    root_side = math.sqrt(cs_mm ** 2 + f_mm ** 2 + p_mm ** 2)

    s_max = l_max - 2 * t_nom_mm   # heel-to-heel distance between terminations

    zmax = l_min + 2 * fillet["Jt"] + root_long
    gmin = s_max - 2 * fillet["Jh"] - root_long
    xmax = w_min + 2 * fillet["Js"] + root_side

    if gmin <= 0:
        raise ValueError(
            f"Gmin={gmin:.3f}mm <= 0 — bu geometri/tolerans kombinasyonuyla "
            "pad'ler çakışıyor demektir; girdi boyutlarını/toleransları "
            "kontrol et."
        )

    pad_length = (zmax - gmin) / 2
    pad_pitch = (zmax + gmin) / 2

    return LandPattern(
        pad_length_mm=round(pad_length, 4),
        pad_width_mm=round(xmax, 4),
        pad_pitch_mm=round(pad_pitch, 4),
        zmax_mm=round(zmax, 4),
        gmin_mm=round(gmin, 4),
        xmax_mm=round(xmax, 4),
        density=density,
    )


def land_pattern(
    component: ChipComponent,
    density: DensityLevel = DensityLevel.B_NOMINAL,
    f_mm: float = DEFAULT_F_MM,
    p_mm: float = DEFAULT_P_MM,
    cl_mm: float = DEFAULT_CL_MM,
    cs_mm: float = DEFAULT_CS_MM,
) -> LandPattern:
    """IPC-7351B pad dimensions for a two-terminal chip component
    (0402/0603/0805/1206 R/C/L and friends).

    For multi-pin packages use ``gullwing_land_pattern``
    (SOIC/TSOP/QFP), ``qfn_land_pattern`` (no-lead), or ``bga_land_pattern``
    (its own, unrelated formula family) instead.
    """
    return _land_pattern_core(
        component.length_nom_mm, component.width_nom_mm,
        component.termination_nom_mm, component.tolerance_mm,
        _FILLET_MM[density], f_mm, p_mm, cl_mm, cs_mm, density)


# --------------------------------------------------------------------------- #
# Common package dimensions (mm) in EIA/IEC naming, for a quick start when no
# datasheet is at hand. IN A REAL PROJECT THE DATASHEET DIMENSION GOVERNS;
# this table is only a coarse starting point for "no data at all".
# --------------------------------------------------------------------------- #
COMMON_CHIP_PACKAGES_MM = {
    "0201": ChipComponent(0.60, 0.30, 0.10, tolerance_mm=0.03),
    "0402": ChipComponent(1.00, 0.50, 0.25, tolerance_mm=0.05),
    "0603": ChipComponent(1.60, 0.80, 0.35, tolerance_mm=0.05),
    "0805": ChipComponent(2.00, 1.25, 0.40, tolerance_mm=0.10),
    "1206": ChipComponent(3.20, 1.60, 0.50, tolerance_mm=0.10),
}


def land_pattern_for_package(
    package: str,
    density: DensityLevel = DensityLevel.B_NOMINAL,
) -> LandPattern:
    """Land pattern for a known package name (e.g. "0402") from
    ``COMMON_CHIP_PACKAGES_MM``. An unknown package raises ``KeyError`` — NO
    DIMENSION IS INVENTED, a real datasheet is required."""
    if package not in COMMON_CHIP_PACKAGES_MM:
        raise KeyError(
            f"'{package}' tanınan paketler arasında değil "
            f"({sorted(COMMON_CHIP_PACKAGES_MM)}). Datasheet'ten "
            "ChipComponent elle oluşturup land_pattern() çağır."
        )
    return land_pattern(COMMON_CHIP_PACKAGES_MM[package], density)


# --------------------------------------------------------------------------- #
# Gullwing / QFP (SOIC, TSOP, TQFP, ...) — multi-pin, TWO OPPOSING ROWS
# --------------------------------------------------------------------------- #
# Mathematically the SAME Zmax/Gmin/Xmax family as the two-terminal chip
# above — the only difference is that L/W/T are not the component BODY but
# the opposing lead rows' OUTER span (lead-tip-to-lead-tip), a single lead's
# width, and the lead foot length. The pitch between neighbouring pads in the
# SAME row does not enter this calculation at all — IPC-7351 does not derive
# pitch, it is taken directly from the datasheet (``pitch_mm`` is only
# carried through for footprint generation).
@dataclass
class GullwingComponent:
    """Gullwing/QFP lead dimensions (mm) taken from the datasheet."""

    pin_count: int
    pitch_mm: float               # centre-to-centre distance of neighbouring leads in one row
    lead_span_nom_mm: float       # L: total outer span, opposing lead tips
    lead_width_nom_mm: float      # W: a single lead's width
    lead_length_nom_mm: float     # T: lead foot length
    tolerance_mm: float = 0.05


def gullwing_land_pattern(
    component: GullwingComponent,
    density: DensityLevel = DensityLevel.B_NOMINAL,
    f_mm: float = DEFAULT_F_MM,
    p_mm: float = DEFAULT_P_MM,
    cl_mm: float = DEFAULT_CL_MM,
    cs_mm: float = DEFAULT_CS_MM,
) -> LandPattern:
    """IPC-7351B land pattern for SOIC/TSOP/QFP (gullwing lead) packages.

    The returned ``LandPattern.pad_pitch_mm`` is the distance BETWEEN THE TWO
    OPPOSING ROWS (driven by lead span); the pitch WITHIN one row is
    ``component.pitch_mm``, which this function does not compute.
    """
    return _land_pattern_core(
        component.lead_span_nom_mm, component.lead_width_nom_mm,
        component.lead_length_nom_mm, component.tolerance_mm,
        _FILLET_MM[density], f_mm, p_mm, cl_mm, cs_mm, density)


# --------------------------------------------------------------------------- #
# QFN (no-lead, pull-back terminal) — SAME math as gullwing, DIFFERENT
# fillet table
# --------------------------------------------------------------------------- #
# A QFN has no visible gullwing foot — the terminal is flush with the body or
# slightly pulled back. The toe fillet (Jt) is therefore NOTICEABLY SMALLER
# than a gullwing's (the pad barely extends past the lead). The values below
# are commonly published typical QFN defaults — same discipline as the
# module-level honesty note: not sufficient for sign-off, cross-check against
# the real IPC-7351B QFN table or the manufacturer's own land pattern
# recommendation on a critical/dense QFN.
_FILLET_MM_QFN = {
    DensityLevel.A_MAXIMUM: {"Jt": 0.30, "Jh": 0.00, "Js": 0.05},
    DensityLevel.B_NOMINAL: {"Jt": 0.20, "Jh": 0.00, "Js": 0.03},
    DensityLevel.C_MINIMUM: {"Jt": 0.10, "Jh": 0.00, "Js": 0.01},
}


def qfn_land_pattern(
    component: GullwingComponent,
    density: DensityLevel = DensityLevel.B_NOMINAL,
    f_mm: float = DEFAULT_F_MM,
    p_mm: float = DEFAULT_P_MM,
    cl_mm: float = DEFAULT_CL_MM,
    cs_mm: float = DEFAULT_CS_MM,
) -> LandPattern:
    """IPC-7351B land pattern for QFN (no-lead) packages.

    ``component.lead_span_nom_mm`` is the body/terminal OUTER span here (a
    QFN's lead span is approximately the body size, there is no protruding
    foot as with a gullwing). ``GullwingComponent`` is REUSED (the same field
    set is enough) — a separate dataclass would only manufacture a fake
    difference; the real difference is the fillet table.
    """
    return _land_pattern_core(
        component.lead_span_nom_mm, component.lead_width_nom_mm,
        component.lead_length_nom_mm, component.tolerance_mm,
        _FILLET_MM_QFN[density], f_mm, p_mm, cl_mm, cs_mm, density)


# --------------------------------------------------------------------------- #
# BGA — a COMPLETELY DIFFERENT formula family (no toe/heel/side fillet)
# --------------------------------------------------------------------------- #
@dataclass
class BgaComponent:
    """BGA ball dimensions (mm) taken from the datasheet."""

    pin_count: int
    pitch_mm: float
    ball_diameter_nom_mm: float


@dataclass
class BgaLandPattern:
    pad_diameter_mm: float
    pitch_mm: float
    mask_type: str   # "NSMD" | "SMD"


def bga_land_pattern(
    component: BgaComponent, mask_type: str = "NSMD",
) -> BgaLandPattern:
    """BGA pad diameter, NOT from a toe/heel/side fillet formula but as a
    RATIO of the ball diameter (IPC-7351B's BGA-specific approach, entirely
    separate from the two-terminal/gullwing family above):

      - NSMD (non-solder-mask-defined, common — the copper itself defines the
        pad boundary, good self-alignment): pad_diameter = 0.80 * ball_diameter
      - SMD (solder-mask-defined, less common — the mask opening is kept
        smaller than the copper and defines the pad boundary):
        pad_diameter = 1.00 * ball_diameter

    HONESTY NOTE (same discipline as the rest of this module): the 0.80/1.00
    multipliers are IPC-7351B's commonly published rules of thumb, not an
    exact physical constant — cross-check against the real IPC-7351B BGA
    table or the JEDEC/manufacturer land pattern recommendation on a
    critical or dense BGA.
    """
    if mask_type not in ("NSMD", "SMD"):
        raise ValueError(
            f"mask_type must be 'NSMD' or 'SMD', got {mask_type!r}")

    multiplier = 0.80 if mask_type == "NSMD" else 1.00
    pad_diameter = round(component.ball_diameter_nom_mm * multiplier, 4)
    if pad_diameter <= 0:
        raise ValueError(
            f"ball_diameter_nom_mm must be positive, got "
            f"{component.ball_diameter_nom_mm}")

    return BgaLandPattern(pad_diameter_mm=pad_diameter,
                          pitch_mm=component.pitch_mm, mask_type=mask_type)
