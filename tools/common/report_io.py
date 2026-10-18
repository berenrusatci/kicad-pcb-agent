"""Report emission, atomic writes, and the shared 0/1/2 exit-code wrapper.

CANONICAL CONVENTIONS enforced here so no tool can forget them:

  stdout   structured JSON only
  stderr   every human/diagnostic message, including crash reports
  exit 0   pass / GREEN
  exit 1   fail / RED / NO_COVERAGE - the tool ran and the answer is "no"
  exit 2   the tool could not produce an answer (bad input, crash, write error)

Encoding is always UTF-8, dumps always use
``ensure_ascii=False, allow_nan=False``. ``allow_nan=False`` matters: NaN and
Infinity are not valid JSON, and three of the board tools used to emit them
silently.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


class ReportWriteError(RuntimeError):
    """The report could not be written; the run must not be reported GREEN."""


def dumps(obj: Any, *, sort_keys: bool = True, indent: int = 2) -> str:
    """The one JSON encoder every tool uses."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=sort_keys,
        indent=indent,
    )


def atomic_write_text(path: Path, text_value: str) -> None:
    """Write via a temp file + fsync + os.replace, so readers never see a
    half-written report."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(text_value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass


def write_report(obj: Any, path: Path | None, *, atomic: bool = True) -> str:
    """Serialise ``obj``; write it to ``path`` when given. Returns the text.

    Raises ReportWriteError so the caller can downgrade the verdict rather
    than exiting 0 with no report on disk.
    """
    text = dumps(obj)
    if path is not None:
        try:
            if atomic:
                atomic_write_text(Path(path), text + "\n")
            else:
                Path(path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise ReportWriteError(f"cannot write {path}: {exc}") from exc
    return text


def _force_utf8(stream):
    """On Windows, a stream attached to a pipe (not a real console) defaults
    to the system codepage (e.g. cp1252), not UTF-8 - so a report containing
    a non-ASCII character (lambda in an escape-geometry detail message, for
    one real case) raises UnicodeEncodeError and the tool exits 2 instead of
    reporting its actual verdict. "Encoding is always UTF-8" above is a
    promise about ``dumps()``'s content, not about the stream it is written
    to; this makes the stream honour it too. A stream without ``reconfigure``
    (e.g. a test double) is left alone.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


# Applied at import time, not just inside emit()/diag(): a self-test path
# (or anything else) that calls bare print() instead of going through this
# module hits the same crash otherwise, and did in practice - one
# self-test's "Sıkı kuplaj" PASS line encoded ``ı`` as cp1254's 0xFD, which
# is not valid UTF-8, and the *parent* test process (correctly decoding as
# UTF-8, per the contract) blew up trying to read it. Every tool imports
# this module before printing anything, so reconfiguring here covers bare
# print() too, not just emit()/diag().
_force_utf8(sys.stdout)
_force_utf8(sys.stderr)


def emit(obj: Any) -> None:
    """Print the structured report to stdout."""
    sys.stdout.write(dumps(obj) + "\n")
    sys.stdout.flush()


def diag(message: str) -> None:
    """Print a human diagnostic to stderr. Never stdout."""
    sys.stderr.write(str(message).rstrip("\n") + "\n")
    sys.stderr.flush()


def error_report(schema_version: str, code: str, message: str,
                 **extra: Any) -> dict:
    """The canonical exit-2 payload shape."""
    report = {
        "schema_version": schema_version,
        "status": "ERROR",
        "effective_status": "ERROR",
        "reason_codes": [code],
        "issues": [{
            "code": code,
            "status": "ERROR",
            "path": "$",
            "message": message,
        }],
        "warnings": [],
        "summary": {"issues": 1, "warnings": 0, "green": False},
    }
    report.update(extra)
    return report


def run_cli(main: Callable[..., int], *args, schema_version: str = "unknown",
            **kwargs) -> int:
    """Run ``main`` and guarantee the 0/1/2 contract.

    Every ``__main__`` block goes through this, so the "wrap it in try/except
    and exit 2" step cannot be omitted the way sch_wire/rewire/wireify used
    to omit it (an unexpected exception there exited 1, indistinguishable
    from a legitimate FAIL).
    """
    try:
        code = main(*args, **kwargs)
    except SystemExit as exc:                     # argparse and friends
        return int(exc.code or 0)
    except KeyboardInterrupt:
        diag("interrupted")
        return EXIT_ERROR
    except ReportWriteError as exc:
        emit(error_report(schema_version, "REPORT_WRITE_ERROR", str(exc)))
        diag(f"ERROR: {exc}")
        return EXIT_ERROR
    except Exception as exc:                      # noqa: BLE001 - never a silent PASS
        emit(error_report(schema_version, "INPUT_ERROR",
                          f"{type(exc).__name__}: {exc}"))
        diag(f"ERROR: {type(exc).__name__}: {exc}")
        return EXIT_ERROR
    if code is None:
        return EXIT_OK
    return int(code)


def status_exit_code(status: str) -> int:
    """Map a verdict string onto the canonical exit code."""
    if status in {"GREEN", "READY", "ACCEPTED", "PASS", "SAFE_TO_THIN"}:
        return EXIT_OK
    if status in {"ERROR", "INVALID_METRIC", "INVALID_COVERAGE"}:
        return EXIT_ERROR
    return EXIT_FAIL
