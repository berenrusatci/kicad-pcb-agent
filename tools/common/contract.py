"""Contract-validation primitives and the structured issue record.

Merged from context_pack (raise ContractError) and gate_receipt (append an
Issue to a Context). Both models are kept because they are genuinely
different: a manifest is rejected wholesale on the first structural error,
while a receipt must accumulate every issue to produce a useful report.
What is shared is the type checking itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ContractError(ValueError):
    """A structural contract violation; aborts validation."""


@dataclass
class Issue:
    """One structured finding. Replaces bare ``"CODE:detail"`` strings."""
    code: str
    status: str          # ERROR / RED / WARN / STALE / NO_COVERAGE ...
    path: str            # JSON pointer-ish location, e.g. "$.inputs[0].sha256"
    message: str
    context: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "status": self.status,
            "path": self.path,
            "message": self.message,
        }
        if self.context:
            out["context"] = self.context
        return out


# --------------------------------------------------------------------------- #
# Type requirements - raise ContractError
# --------------------------------------------------------------------------- #
def require_dict(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{name} must be an object")
    return value


def require_list(value, name: str, nonempty: bool = True) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(
            f"{name} must be a{' non-empty' if nonempty else ''} list")
    return value


def require_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be non-empty text")
    return value


def require_string_list(value, name: str, nonempty: bool = True,
                        unique: bool = True) -> list[str]:
    items = require_list(value, name, nonempty=nonempty)
    for i, item in enumerate(items):
        require_text(item, f"{name}[{i}]")
    if unique and len(items) != len(set(items)):
        raise ContractError(f"{name} contains duplicates")
    return items


def require_int(value, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) \
            or (minimum is not None and value < minimum):
        suffix = f" >= {minimum}" if minimum is not None else ""
        raise ContractError(f"{name} must be an integer{suffix}")
    return value


def require_number(value, name: str) -> float:
    """A finite JSON number. Rejects bool, which is an int in Python."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ContractError(f"{name} must be finite")
    return value


# --------------------------------------------------------------------------- #
# Type checks - return bool, for accumulating validators
# --------------------------------------------------------------------------- #
def is_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_finite_number(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    value = float(value)
    return value == value and value not in (float("inf"), float("-inf"))
