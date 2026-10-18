#!/usr/bin/env python3
"""JLC2KiCadLib bridge: fetch a part's KiCad symbol/footprint/3D model from
its LCSC part number, with a local success cache so a repeat request for the
same (part, destination) pair never re-hits the network.

backend: cli  (external tool: the ``JLC2KiCadLib`` PyPI package; network:
LCSC/EasyEDA, via that tool - not called directly by this module)

REAL CLI, VERIFIED (not guessed)
---------------------------------
Unlike ``jlcpcb_dfm.py``'s private-API guess, ``JLC2KiCadLib`` is a real,
published PyPI package (https://pypi.org/project/JLC2KiCadLib/) with a
documented CLI. The flags used here (``-dir``, ``-symbol_lib``,
``--skip_existing``, ``--version``) were read from that package's own
``--help`` output as published on PyPI at the time this was written -
this is the one piece of this bridge that is NOT a placeholder. Pin the
installed version if the flag set ever changes upstream.

THE CACHE IS A SUCCESS MARKER, NOT A FILE COPY
-------------------------------------------------
This module does NOT track or copy the exact symbol/footprint/3D-model
files JLC2KiCadLib writes - enumerating them ahead of time (or by diffing a
directory) would be a guess about internal tool behaviour, exactly the kind
of invented detail this bundle avoids. Instead it records "this LCSC part
was already fetched into this destination, with this symbol library name"
and skips re-running the network-touching subprocess for that same request
before it happens again. A DIFFERENT output directory or symbol library
name is a cache miss, and correctly re-downloads.

FAIL-CLOSED, NO INVENTED SUCCESS
-----------------------------------
A missing ``JLC2KiCadLib`` installation, a non-zero exit code, or a
timeout all raise ``JlcLibraryUnavailable`` - never a fabricated
``ComponentDownload(ok=True)``.

Usage:
    jlc_library.py C12345 [--output-dir ./lib] [--symbol-lib JLC2KiCad_lib]
                   [--force] [--json out.json]
    jlc_library.py --self-test

Exit: 0 = fetched (or already cached), 1 = fetch failed / tool unavailable,
2 = bad input.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.memory.store import jlc_parts_cache_dir  # noqa: E402

SCHEMA_VERSION = "jlc-library-report/v1"
TOOL_NAME = "jlc_library"

DEFAULT_SYMBOL_LIB = "JLC2KiCad_lib"


class JlcLibraryUnavailable(RuntimeError):
    """JLC2KiCadLib is not installed, or the fetch could not be trusted.

    A distinct type so a caller can tell "this part was not fetched" apart
    from an unrelated failure and mark the step NEEDS_HUMAN instead of
    guessing.
    """


@dataclass
class ComponentDownload:
    lcsc_code: str
    output_dir: str
    symbol_lib: str
    ok: bool
    from_cache: bool
    stdout: str = ""
    stderr: str = ""


def jlc2kicadlib_available(python: str | None = None, timeout_s: int = 20) -> bool:
    """Checked, never assumed - mirrors ``production/ngspice.py``'s
    ``find_ngspice``/``java_available`` pattern."""
    interpreter = python or sys.executable
    try:
        proc = subprocess.run(
            [interpreter, "-m", "JLC2KiCadLib", "--version"],
            capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _cache_key(lcsc_code: str, symbol_lib: str, output_dir: str) -> str:
    """Deterministic, filesystem-safe key for the (part, destination) pair."""
    safe_dir = str(Path(output_dir).resolve()).replace(":", "").replace("\\", "_").replace("/", "_")
    return f"{lcsc_code}__{symbol_lib}__{safe_dir}"


def _marker_path(lcsc_code: str, symbol_lib: str, output_dir: str,
                 cache_root: str | Path | None = None) -> Path:
    cache_dir = jlc_parts_cache_dir(cache_root, create=True)
    return cache_dir / f"{_cache_key(lcsc_code, symbol_lib, output_dir)}.json"


def is_cached(lcsc_code: str, symbol_lib: str, output_dir: str,
             cache_root: str | Path | None = None) -> bool:
    return _marker_path(lcsc_code, symbol_lib, output_dir, cache_root).is_file()


def _write_marker(lcsc_code: str, symbol_lib: str, output_dir: str,
                  cache_root: str | Path | None = None) -> None:
    marker = _marker_path(lcsc_code, symbol_lib, output_dir, cache_root)
    marker.write_text(json.dumps({
        "lcsc_code": lcsc_code,
        "symbol_lib": symbol_lib,
        "output_dir": str(Path(output_dir)),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")


def download_component(
        lcsc_code: str, output_dir: str = "./lib",
        symbol_lib: str = DEFAULT_SYMBOL_LIB, force: bool = False,
        python: str | None = None, cache_root: str | Path | None = None,
        timeout_s: int = 120) -> ComponentDownload:
    """Fetch (or reuse a cached fetch of) one LCSC part's KiCad library
    entry. See the module docstring for what the cache does and does not
    track.
    """
    if not force and is_cached(lcsc_code, symbol_lib, output_dir, cache_root):
        return ComponentDownload(lcsc_code, output_dir, symbol_lib,
                                 ok=True, from_cache=True,
                                 stdout="(cache hit - not re-downloaded)")

    if not jlc2kicadlib_available(python):
        raise JlcLibraryUnavailable(
            "download_component: JLC2KiCadLib is not importable "
            f"(`{python or sys.executable} -m JLC2KiCadLib --version` "
            "failed). Install it with `pip install JLC2KiCadLib`. No part "
            "was fetched.")

    command = [python or sys.executable, "-m", "JLC2KiCadLib",
              "-dir", output_dir, "-symbol_lib", symbol_lib,
              "--skip_existing", lcsc_code]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise JlcLibraryUnavailable(
            f"download_component: JLC2KiCadLib did not finish within "
            f"{timeout_s}s for {lcsc_code}") from exc

    if proc.returncode != 0:
        raise JlcLibraryUnavailable(
            f"download_component: JLC2KiCadLib exited {proc.returncode} "
            f"for {lcsc_code}: {proc.stderr.strip()[-500:]}")

    _write_marker(lcsc_code, symbol_lib, output_dir, cache_root)
    return ComponentDownload(lcsc_code, output_dir, symbol_lib, ok=True,
                             from_cache=False, stdout=proc.stdout,
                             stderr=proc.stderr)


def self_test() -> list[str]:
    """Cache-key logic and fail-closed paths only. Does NOT run
    JLC2KiCadLib or touch the network."""
    problems: list[str] = []

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        # FAULT INJECTION: JLC2KiCadLib "not installed" must refuse, not
        # silently report a fabricated success.
        try:
            download_component("C99999", output_dir=tmp, cache_root=tmp,
                               python=sys.executable + "_definitely_missing")
        except JlcLibraryUnavailable:
            pass
        else:
            problems.append(
                "fault injection did not hold: a missing interpreter did "
                "not raise JlcLibraryUnavailable")

        # A cache write followed by a cache hit must skip re-downloading -
        # proven by NOT mocking a subprocess at all: if this reached
        # subprocess.run, jlc2kicadlib_available's real availability check
        # against sys.executable (no such module) would raise, which the
        # test above already confirmed. Reaching `ok=True` here without an
        # exception proves the cache path, not the download path, ran.
        _write_marker("C12345", DEFAULT_SYMBOL_LIB, tmp, cache_root=tmp)
        if not is_cached("C12345", DEFAULT_SYMBOL_LIB, tmp, cache_root=tmp):
            problems.append("a written marker was not read back as cached")
        cached = download_component("C12345", output_dir=tmp, cache_root=tmp)
        if not (cached.ok and cached.from_cache):
            problems.append(f"a cache hit did not short-circuit: {cached}")

        # A different destination is a genuine cache MISS.
        if is_cached("C12345", DEFAULT_SYMBOL_LIB, tmp + "_other", cache_root=tmp):
            problems.append("a different output_dir was incorrectly cache-hit")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lcsc_code", nargs="?",
                        help="LCSC part number, e.g. C12345")
    parser.add_argument("--output-dir", default="./lib")
    parser.add_argument("--symbol-lib", default=DEFAULT_SYMBOL_LIB)
    parser.add_argument("--force", action="store_true",
                        help="ignore the cache and re-fetch")
    parser.add_argument("--cache-root",
                        help="override the cache location "
                             "($PCB_AGENT_MEMORY_DIR by default)")
    parser.add_argument("--timeout-s", type=int, default=120)
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: jlc_library")
        return 0

    if not args.lcsc_code:
        parser.error("the LCSC part number is required")

    try:
        outcome = download_component(
            args.lcsc_code, args.output_dir, args.symbol_lib, args.force,
            cache_root=args.cache_root, timeout_s=args.timeout_s)
        status = "PASS"
        detail = ("served from cache" if outcome.from_cache
                  else "fetched from JLC2KiCadLib")
    except JlcLibraryUnavailable as exc:
        diag(f"NO COVERAGE: {exc}")
        status = "NO_COVERAGE"
        detail = str(exc)
        outcome = ComponentDownload(args.lcsc_code, args.output_dir,
                                    args.symbol_lib, ok=False, from_cache=False)

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "lcsc_code": args.lcsc_code,
        "status": status,
        "detail": detail,
        "from_cache": outcome.from_cache,
        "output_dir": args.output_dir,
        "symbol_lib": args.symbol_lib,
    }
    write_report(report, args.json)
    emit(report)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
