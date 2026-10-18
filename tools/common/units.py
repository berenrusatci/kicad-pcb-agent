"""Unit constants and KiCad internal-unit conversion.

Hoisted from four byte-identical copies of ``MM``/``mm``/``xy`` in
dfm_emc_check, mask_dam, stitch_density and via_stub.

NOTE ON A FIXED DIVERGENCE
--------------------------
The four copies were not actually identical: ``via_stub.xy`` returned
``(round(mm(p.x), 4), round(mm(p.y), 4))`` while the other three returned
unrounded millimetres. That silent fork made geometry results incomparable
between tools. The canonical ``xy`` here is **unrounded**; callers that want
the old via_stub presentation must say so explicitly via ``xy_rounded`` (or
``round_xy``), so the rounding is visible at the call site.
"""

from __future__ import annotations

# KiCad internal units per millimetre (nm) for the pcbnew SWIG API.
MM = 1e6

# Physical constants
C_MPS = 299_792_458.0        # speed of light, m/s
C_MM_PS = 0.299792458        # speed of light, mm/ps

# Copper / imperial conversions
OZ_TO_MM = 0.0348            # 1 oz copper ~= 34.8 um
MM_TO_MIL = 39.3701


def mm(value: float) -> float:
    """KiCad internal units -> millimetres."""
    return value / MM


def to_iu(value_mm: float) -> float:
    """Millimetres -> KiCad internal units."""
    return value_mm * MM


def xy(point) -> tuple[float, float]:
    """pcbnew VECTOR2I -> (x_mm, y_mm), unrounded.

    This is the canonical form. Do not add rounding here; see module docstring.
    """
    return (mm(point.x), mm(point.y))


def round_xy(point_mm: tuple[float, float], digits: int = 4) -> tuple[float, float]:
    """Explicitly round an already-converted (x_mm, y_mm) pair."""
    return (round(point_mm[0], digits), round(point_mm[1], digits))


def xy_rounded(point, digits: int = 4) -> tuple[float, float]:
    """Convenience: convert then round. Rounding is explicit at the call site."""
    return round_xy(xy(point), digits)
