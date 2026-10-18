"""Screening knobs read from ``fixtures/references/tuning_defaults.json``.

WHY THIS MODULE EXISTS
----------------------
The same physical rule was written in three places with two different
numbers, and nothing noticed:

    scripts/board/decoupling_coverage.py  DEFAULT_MAX_DISTANCE_MM = 3.0
    scripts/board/measure_board.py        --decoupling-max-mm default 1.5
    scripts/sim/ngspice.py                DEFAULT_MAX_DECOUPLING_DISTANCE_MM = 1.5

``ngspice.py``'s copy even carried a comment saying it was kept "as one
source rather than a second, independently-drifting constant" - while a
third constant, with a different value, sat in ``decoupling_coverage.py``
under a comment admitting the two were "NOT reconciled". Two of this
bundle's own tools could therefore reach opposite verdicts about the same
capacitor, and which one you believed depended on which tool you ran.

The fix is the one ``common.stackup.fab_profiles`` already uses: the number
lives in a data file, is read at call time, and has NO built-in fallback -
a silently substituted screening threshold is exactly the invisible wrong
answer this bundle exists to prevent.

WHAT THIS MODULE IS NOT: a claim that these values are right. They are the
values the code already had, moved somewhere they can be changed once and
audited. Each entry in the JSON carries the rationale the code stated, and
says so explicitly where the code stated none.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TUNING_SCHEMA = "tuning-defaults/v1"
DEFAULT_TUNING_PATH = (Path(__file__).resolve().parent.parent
                       / "fixtures" / "references" / "tuning_defaults.json")

_CACHE: Dict[Path, Dict[str, Any]] = {}


class TuningDataError(ValueError):
    """The tuning table is missing, unreadable, or does not carry the value
    that was asked for. Raised rather than defaulted: a screening threshold
    that quietly falls back to a built-in number turns a gate into a guess."""


def load_tuning(path: Optional[Path] = None) -> Dict[str, Any]:
    """The parsed table. Cached per path; call ``clear_cache()`` in a test
    that writes its own file."""
    resolved = Path(path) if path is not None else DEFAULT_TUNING_PATH
    if resolved in _CACHE:
        return _CACHE[resolved]

    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TuningDataError(
            f"tuning table missing: {resolved}. There is deliberately no built-in "
            "fallback - a silently substituted screening threshold would turn a "
            "gate into a guess.") from exc
    except (OSError, ValueError) as exc:
        raise TuningDataError(f"cannot read {resolved}: {exc}") from exc

    if raw.get("schema_version") != TUNING_SCHEMA:
        raise TuningDataError(
            f"{resolved}: expected schema_version {TUNING_SCHEMA!r}, "
            f"got {raw.get('schema_version')!r}")

    _CACHE[resolved] = raw
    return raw


def clear_cache() -> None:
    _CACHE.clear()


def tuning_value(section: str, key: str, path: Optional[Path] = None) -> Any:
    """One entry's ``value``.

    A missing section/key RAISES. It does not return ``None`` for a caller
    to coalesce away - that is how a threshold becomes zero without anyone
    deciding it should be.
    """
    table = load_tuning(path)
    entry = (table.get(section) or {}).get(key)
    if entry is None:
        raise TuningDataError(
            f"{section}.{key} is not in the tuning table "
            f"({path or DEFAULT_TUNING_PATH}) - add it there rather than "
            "reintroducing a literal default.")
    if not isinstance(entry, dict) or "value" not in entry:
        raise TuningDataError(
            f"{section}.{key} must be an object carrying 'value' (and its rationale), "
            f"got {entry!r}")
    return entry["value"]


def tuning_float(section: str, key: str, path: Optional[Path] = None) -> float:
    value = tuning_value(section, key, path)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TuningDataError(f"{section}.{key} must be a number, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------- #
# Named accessors - so a caller asks for a rule, not for a dictionary path
# --------------------------------------------------------------------------- #
def decoupling_limits(path: Optional[Path] = None) -> Tuple[float, float]:
    """``(violation limit mm, placement target mm)``.

    TWO numbers, not one, because the code's own notes described two
    different things: 3.0 mm as "a datasheet-typical upper bound" and 1.5 mm
    as "a tighter placement target". They were being used interchangeably as
    the failure threshold, which is what made them look like a contradiction
    instead of a limit and a goal.
    """
    limit = tuning_float("decoupling", "max_distance_mm", path)
    target = tuning_float("decoupling", "target_distance_mm", path)
    if target > limit:
        raise TuningDataError(
            f"decoupling target ({target}mm) is looser than the limit ({limit}mm) - "
            "the target is the goal, it cannot sit beyond the failure threshold.")
    return limit, target


def routing_costs(path: Optional[Path] = None) -> Dict[str, float]:
    return {key: tuning_float("routing", key, path)
            for key in ("rule_multiplier", "violation_weight", "via_cost")}


def ambient_temp_c(path: Optional[Path] = None) -> float:
    return tuning_float("thermal", "ambient_temp_c", path)


def drop_pct_tiers(path: Optional[Path] = None) -> Tuple[Tuple[float, Tuple[float, ...]], ...]:
    """``((max_drop_pct, (farads, ...)), ...)`` ordered as written.

    ``null`` in the file becomes ``inf`` - the open-ended top tier. Encoding
    it as ``null`` keeps the JSON valid (JSON has no infinity literal)
    without the file having to carry a magic large number.
    """
    tiers = tuning_value("pdn", "drop_pct_tiers", path)
    if not isinstance(tiers, list) or not tiers:
        raise TuningDataError("pdn.drop_pct_tiers must be a non-empty list")

    out: List[Tuple[float, Tuple[float, ...]]] = []
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            raise TuningDataError(f"pdn.drop_pct_tiers[{index}] must be an object")
        threshold = tier.get("max_drop_pct")
        caps = tier.get("capacitors_f")
        if not isinstance(caps, list) or not caps:
            raise TuningDataError(
                f"pdn.drop_pct_tiers[{index}].capacitors_f must be a non-empty list")
        out.append((float("inf") if threshold is None else float(threshold),
                    tuple(float(c) for c in caps)))

    if [t for t, _ in out] != sorted(t for t, _ in out):
        raise TuningDataError(
            "pdn.drop_pct_tiers must be ordered by ascending max_drop_pct - the "
            "lookup takes the FIRST tier whose threshold covers the drop, so an "
            "out-of-order table silently returns the wrong tier.")
    return tuple(out)
