"""Path confinement.

Merged from three implementations with three different strengths:

  - context_pack.safe_path            reject absolute + ".." then resolve
  - gate_receipt.resolve_path         resolve then relative_to(root)
  - research_promotion_gate.validated_report_path
                                      LEXICAL *and* RESOLVED check + allowlist

The third is the strict one: checking only the resolved path lets a symlink
inside the root point outside it, while checking only the lexical path lets a
symlink outside the root be reached by a name that looks contained. Both are
required, so both are done here.
"""

from __future__ import annotations

import os
from pathlib import Path


class PathConfinementError(ValueError):
    """A path escaped its confinement root."""


def safe_path(root: Path, relative: str, field: str = "path") -> Path:
    """Resolve ``relative`` under ``root``, refusing any escape.

    Rejects absolute paths and ``..`` components up front, then verifies the
    result both lexically and after symlink resolution.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise PathConfinementError(f"{field} must be non-empty text")
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise PathConfinementError(f"path escapes root: {relative}")
    root = Path(root).resolve()
    candidate = root / raw
    lexical = Path(os.path.abspath(candidate))
    resolved = candidate.resolve(strict=False)
    if not lexical.is_relative_to(root) or not resolved.is_relative_to(root):
        raise PathConfinementError(f"path escapes root: {relative}")
    return resolved


def resolve_under_root(raw: str, base: Path, root: Path) -> Path:
    """Resolve ``raw`` relative to ``base`` and confine it to ``root``.

    ``base`` is typically the directory of the file that named the path
    (a receipt names its artifacts relative to itself), while ``root`` is the
    outer confinement boundary.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PathConfinementError("path must be non-empty text")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise PathConfinementError(f"absolute path not allowed: {raw}")
    root = Path(root).resolve()
    joined = Path(base) / candidate
    lexical = Path(os.path.abspath(joined))
    resolved = joined.resolve(strict=False)
    if not lexical.is_relative_to(root) or not resolved.is_relative_to(root):
        raise PathConfinementError(f"path escapes root {root}: {raw}")
    return resolved


def confine_to_dir(requested: Path, allowed_dir: Path,
                   suffix: str | None = None) -> Path:
    """Confine an OUTPUT path to ``allowed_dir``, optionally pinning a suffix.

    Used for report writes, where the tool must not be usable as an arbitrary
    file-write primitive.
    """
    requested = Path(requested)
    if suffix is not None and requested.suffix.casefold() != suffix.casefold():
        raise PathConfinementError(f"output must use the {suffix} suffix")
    allowed_dir = Path(allowed_dir).resolve()
    lexical = Path(os.path.abspath(requested))
    resolved = requested.resolve(strict=False)
    if not lexical.is_relative_to(allowed_dir) \
            or not resolved.is_relative_to(allowed_dir):
        raise PathConfinementError(
            f"output is restricted to {allowed_dir}: {requested}")
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """Lexical AND resolved containment test."""
    root = Path(root).resolve()
    lexical = Path(os.path.abspath(path))
    resolved = Path(path).resolve(strict=False)
    return lexical.is_relative_to(root) and resolved.is_relative_to(root)
