"""SHA-256 helpers.

Hoisted from four near-identical copies:
  context_pack.sha256, gate_receipt.sha256_file,
  research_promotion_gate.sha256_path, tests/test_gate_receipt.sha256
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_file(path: Path | str) -> str:
    """Streaming SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_digest(value: object) -> bool:
    """True for a lowercase-or-uppercase 64-char hex string."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value.lower())
    )


def canonical_digest(value: object) -> str:
    """Digest of a JSON value under a stable key order.

    Not RFC 8785 JCS; this is the same sort_keys/separators convention the
    v2 research receipt already used, kept byte-compatible on purpose.
    """
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return sha256_text(encoded)
