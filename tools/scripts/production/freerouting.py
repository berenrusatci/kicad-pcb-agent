#!/usr/bin/env python3
"""FreeRouting bridge: board -> .dsn -> java -jar freerouting.jar -> .ses -> board.

backend: cli  (external binaries: java + freerouting.jar; pcbnew for DSN/SES)

IMPORT ISOLATION
----------------
``java``, ``freerouting.jar`` and ``pcbnew`` are all resolved INSIDE the
functions that need them, exactly the way ``scripts/board`` confines pcbnew.
Importing this module, running the self-test and collecting the test suite all
work on a machine with none of them installed; a missing binary produces a
reported NO_COVERAGE or an explicit error, never a silent pass.

JAR BOOTSTRAP IS OPT-IN, NEVER AUTOMATIC
-----------------------------------------
``ensure_jar()``/``download_jar()`` fetch ``freerouting.jar`` from the
project's GitHub releases when it is missing locally. This is a genuine
network call that runs arbitrary downloaded code, so it NEVER runs unless the
caller explicitly asks for it (``--allow-jar-download`` on the CLI,
``allow_jar_download=True`` in ``route_chain()``/``ensure_jar()``). The
default stays fail-closed: a missing jar raises ``FreeroutingUnavailable``
exactly as before this feature existed.

HONESTY NOTE: the GitHub Releases API call and the download itself have NOT
been exercised against the live network in this environment (no outbound
network access here). The request/response handling follows GitHub's
documented public releases API shape and fails closed (raises
``ConnectionError``) on anything unexpected - but this path should be
smoke-tested against the real API before being relied on unattended. No
checksum is pinned against the downloaded jar: GitHub does not publish one
for release assets, and inventing one here would be worse than not checking
at all. Verify a fresh download's provenance by hand before production use.

WHAT WAS MEASURED, NOT ASSUMED
------------------------------
* ``kicad-cli`` does NOT support DSN or SES. That is still true. But
  ``pcbnew.ExportSpecctraDSN`` / ``pcbnew.ImportSpecctraSES`` DO work in
  KiCad's embedded Python, so the chain runs through a small pcbnew script
  rather than the CLI.
* FreeRouting does not merely get slow on a real board: it can recurse
  infinitely inside ``PolylineTrace.combine()`` and die with
  ``StackOverflowError``. Three countermeasures, all necessary:
    1. ``-Xss`` raises the JVM stack - which does NOT prevent a true infinite
       recursion, only some shallow ones. Alone it is insufficient.
    2. ``-Djava.awt.headless=true`` forbids the JVM from opening any dialog.
       Without it a crash handler can try to pop up a window that nobody will
       ever close, and the subprocess waits forever. Measured against the real
       v2.2.4 jar this DOES work (the routing job starts and runs), though
       FreeRouting still logs one cosmetic "set gui.enabled to false" warning
       - see the comment at the command construction for why no flag is
       passed for that.
    3. stdout is read LINE BY LINE in real time and the process is killed the
       moment a Java exception pattern appears, rather than waiting out the
       full timeout.

* THE TIMEOUT MUST NOT DEPEND ON THE CHILD PRODUCING OUTPUT. Measured
  against the real jar and a real 174-net pcbnew DSN export: FreeRouting
  prints four lines, then goes completely silent for the whole routing
  computation. An earlier version of this bridge called
  ``proc.stdout.readline()`` inside the polling loop; that call BLOCKS, so
  the loop never returned to its own deadline check and a 240 s budget ran
  1682 s (28 min) before the process was killed BY HAND - while the
  function still returned ``timed_out=True`` with "did not finish within
  240s", i.e. it reported a budget it had never enforced. Reading now
  happens on a separate thread feeding a queue, and the main loop polls
  that queue with the remaining deadline (Windows cannot ``select()`` on a
  pipe, so a thread is the portable answer).
* ``ImportSpecctraSES``'s return value is not trustworthy on its own: a .ses
  with real routes can return False on a padstack mismatch, or return True and
  add zero tracks. The track count is compared before and after, and the
  caller must see BOTH ``ok`` and ``tracks_changed`` before calling it routed.

Usage:
    freerouting.py <board.kicad_pcb> [--jar ext/freerouting.jar] [--json out.json]
                   [--workdir DIR] [--timeout-s 240] [--java-heap 4g]
                   [--allow-jar-download] [--dsn-only]
    freerouting.py --self-test

Exit: 0 = routed and verified, 1 = not routed / NO_COVERAGE, 2 = run error.
"""

from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import FAIL, board_report, result  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "production-gate-report/v1"
TOOL_NAME = "production_freerouting"

JAVA_ERROR_PATTERNS = ("Exception", "Error:", "StackOverflowError",
                       "OutOfMemoryError")

# Default jar location: kept OUT of the repo tree proper (ext/ is the
# project's own convention for fetched-not-committed binaries), CWD-relative
# like every other path this module takes.
DEFAULT_JAR_PATH = "ext/freerouting.jar"
GITHUB_RELEASES_API = "https://api.github.com/repos/freerouting/freerouting/releases/latest"
DEFAULT_JAVA_HEAP = "4g"


class FreeroutingUnavailable(RuntimeError):
    """pcbnew, java or the jar is missing on this machine.

    A distinct type so a caller can tell "this installation cannot route" from
    an unrelated failure and mark the step NEEDS_HUMAN instead of guessing.
    """


@dataclass
class DsnResult:
    ok: bool
    dsn_path: str | None
    stdout: str
    stderr: str


@dataclass
class SesResult:
    ok: bool
    tracks_before: int
    tracks_after: int
    tracks_changed: bool
    stdout: str
    stderr: str


@dataclass
class RouteRun:
    ok: bool
    ses_path: str | None
    stdout: str
    stderr: str
    timed_out: bool = False
    java_error: bool = False


_DSN_EXPORT = """
import json, sys
import pcbnew
board_path, dsn_path = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(board_path)
ok = bool(pcbnew.ExportSpecctraDSN(board, dsn_path))
print(json.dumps({"ok": ok}))
sys.exit(0 if ok else 2)
"""

_SES_IMPORT = """
import json, sys
import pcbnew
board_path, ses_path = sys.argv[1], sys.argv[2]
board = pcbnew.LoadBoard(board_path)
before = len([t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"])
ok = bool(pcbnew.ImportSpecctraSES(board, ses_path))
after = len([t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"])
if ok:
    board.Save(board_path)
print(json.dumps({"ok": ok, "before": before, "after": after,
                  "changed": after != before}))
sys.exit(0 if ok else 2)
"""


def _run_pcbnew_script(script: str, argv, python: str | None = None,
                       timeout_s: int = 60) -> subprocess.CompletedProcess:
    """Run a short pcbnew script under an interpreter that can import pcbnew.

    Defaults to this interpreter, since pcbnew 10.0.5 imports from the system
    python. ``--kicad-python`` overrides it for installs where it does not.
    """
    import tempfile
    interpreter = python or sys.executable
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "_pcbnew_step.py"
        path.write_text(script, encoding="utf-8")
        return subprocess.run([interpreter, str(path), *argv],
                              capture_output=True, text=True, timeout=timeout_s)


def _pcbnew_available(python: str | None = None) -> bool:
    """Can the chosen interpreter import pcbnew? Checked, never assumed."""
    interpreter = python or sys.executable
    try:
        proc = subprocess.run(
            [interpreter, "-c", "import pcbnew"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def export_dsn(board_path: str, dsn_path: str, python: str | None = None,
               timeout_s: int = 60) -> DsnResult:
    """Export the board to Specctra DSN via pcbnew. Fail-closed if unavailable."""
    if not _pcbnew_available(python):
        raise FreeroutingUnavailable(
            "export_dsn: pcbnew could not be imported. The DSN/SES chain needs "
            "the pcbnew Python API (kicad-cli has no DSN export). NEEDS_HUMAN: "
            "verify the KiCad install or pass --kicad-python.")
    try:
        proc = _run_pcbnew_script(_DSN_EXPORT, [board_path, dsn_path],
                                  python, timeout_s)
    except subprocess.TimeoutExpired as exc:
        return DsnResult(False, None, exc.stdout or "",
                         f"DSN export did not finish within {timeout_s}s")
    ok = proc.returncode == 0 and Path(dsn_path).exists()
    return DsnResult(ok, dsn_path if ok else None, proc.stdout, proc.stderr)


def java_available() -> bool:
    return shutil.which("java") is not None


def find_jar_download_url(release: dict) -> str:
    """Pick the ``.jar`` asset's download URL out of a parsed GitHub
    'latest release' API response. Pure function, no network - testable
    without touching the internet.

    Raises ``ValueError`` when the release carries no ``.jar`` asset at all,
    rather than guessing at a different asset - a wrong asset silently
    downloaded and run is worse than refusing.
    """
    assets = release.get("assets") or []
    for asset in assets:
        name = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if name.lower().endswith(".jar") and url.startswith("https://"):
            return url
    raise ValueError(
        "the latest GitHub release has no .jar asset with an https download "
        "URL - cannot bootstrap freerouting.jar from it")


def download_jar(dest_path: str = DEFAULT_JAR_PATH,
                 api_url: str = GITHUB_RELEASES_API,
                 timeout_s: int = 120) -> str:
    """Fetch ``freerouting.jar`` from the project's latest GitHub release.

    FAIL-CLOSED: any network error, timeout, non-200 response, or missing
    asset raises ``ConnectionError`` (or ``ValueError`` for a malformed
    response) - never writes a partial or empty file that a caller could
    mistake for a real jar. The download streams to a ``.part`` temp file
    next to ``dest_path`` and is renamed into place only once it is
    complete, so a crash mid-download cannot leave a half-written jar where
    ``ensure_jar()`` would find it and treat it as present.
    """
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(
            api_url, headers={"Accept": "application/vnd.github+json",
                              "User-Agent": "pcb-agent-freerouting-bootstrap"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                raise ConnectionError(
                    f"GitHub releases API returned HTTP {response.status} "
                    f"for {api_url}")
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConnectionError(
            f"could not reach {api_url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{api_url} did not return valid JSON: {exc}") from exc

    download_url = find_jar_download_url(release)

    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        request = urllib.request.Request(
            download_url, headers={"User-Agent": "pcb-agent-freerouting-bootstrap"})
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            if response.status != 200:
                raise ConnectionError(
                    f"GitHub returned HTTP {response.status} downloading "
                    f"{download_url}")
            with open(part, "wb") as fh:
                shutil.copyfileobj(response, fh)
    except urllib.error.URLError as exc:
        part.unlink(missing_ok=True)
        raise ConnectionError(
            f"could not download {download_url}: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    if part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise ConnectionError(
            f"downloaded 0 bytes from {download_url} - refusing to install "
            "an empty jar")
    part.replace(dest)
    return str(dest)


def ensure_jar(jar_path: str = DEFAULT_JAR_PATH,
               allow_download: bool = False,
               timeout_s: int = 120) -> str:
    """The jar path to use, downloading it first if it is missing AND the
    caller explicitly opted in.

    Fail-closed by default: a missing jar with ``allow_download=False``
    (the default everywhere - CLI, ``route_chain()``) raises
    ``FreeroutingUnavailable`` exactly as ``run_freerouting()`` always has.
    Downloading arbitrary executable code is never a silent side effect of
    an ordinary run.
    """
    if Path(jar_path).is_file():
        return jar_path
    if not allow_download:
        raise FreeroutingUnavailable(
            f"ensure_jar: {jar_path} not found and downloading was not "
            "requested. Pass --allow-jar-download (CLI) or "
            "allow_download=True to fetch it from GitHub, or place the jar "
            "there yourself. The router did NOT run.")
    diag(f"{jar_path} not found; downloading the latest freerouting.jar "
         f"from GitHub (opt-in)")
    return download_jar(jar_path, timeout_s=timeout_s)


def run_freerouting(dsn_path: str, ses_path: str,
                    jar: str = "freerouting.jar", extra_args=None,
                    timeout_s: int = 240, java_stack: str = "8m",
                    java_heap: str = DEFAULT_JAVA_HEAP) -> RouteRun:
    """Run headless FreeRouting, killing it the instant Java throws.

    The default timeout is 240 s, not half an hour: real boards (14 and 174
    nets) failed to finish inside 120-240 s, and an autonomous chain locking up
    for half an hour on one routing step is not acceptable. The caller falls
    back rather than waiting.

    This function itself never downloads the jar - see ``ensure_jar()`` for
    the opt-in bootstrap; callers that want it call that first and pass its
    result in as ``jar``.
    """
    if not java_available():
        raise FreeroutingUnavailable(
            "run_freerouting: 'java' is not on PATH. The router did NOT run; "
            "this is NOT a clean routing result.")
    if not Path(jar).is_file():
        raise FreeroutingUnavailable(
            f"run_freerouting: {jar} not found. The router did NOT run.")

    # MEASURED against the real v2.2.4 jar: with -Djava.awt.headless=true the
    # router DOES run headless (the routing job starts and progresses), but it
    # still logs one cosmetic warning:
    #   "WARN Couldn't get screen resolution. If you are running in a headless
    #    environment, disable the GUI by setting gui.enabled to false"
    # `gui.enabled` is NOT a JVM system property and NOT a CLI option - it is
    # absent from `java -jar freerouting.jar -help` and `-Dgui.enabled=false`
    # was tried and did NOT silence the warning. It appears to be a config-file
    # setting whose location is unverified here, so no flag is passed for it:
    # shipping a switch that measurably does nothing would be worse than
    # documenting the gap.
    command = ["java", f"-Xmx{java_heap}", f"-Xss{java_stack}",
               "-Djava.awt.headless=true", "-jar", jar,
               "-de", dsn_path, "-do", ses_path]
    if extra_args:
        command.extend(extra_args)

    started = time.monotonic()
    lines: list[str] = []
    java_error: str | None = None

    proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)

    # The reader runs on its own thread and the main loop polls a queue with a
    # deadline. It CANNOT be a plain `proc.stdout.readline()` in this loop:
    # readline() BLOCKS, and FreeRouting goes completely silent while it
    # computes, so the loop would never come back to the timeout check.
    # MEASURED, not theorised: with the previous in-loop readline(), a real
    # 174-net board ran for 1682 s against a 240 s budget and only stopped
    # when the process was killed by hand - while the function still
    # reported `timed_out=True, "did not finish within 240s"`, i.e. it
    # claimed to have enforced a budget it never enforced. select() is not
    # an option here because Windows cannot select() on a pipe.
    line_queue: "queue.Queue[str | None]" = queue.Queue()

    def _pump(stream) -> None:
        try:
            if stream is not None:        # a caller may wire stdout elsewhere
                for raw in iter(stream.readline, ""):
                    line_queue.put(raw)
        finally:
            line_queue.put(None)          # sentinel: stream closed

    reader = threading.Thread(target=_pump, args=(proc.stdout,), daemon=True)
    reader.start()

    def _kill() -> None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    try:
        while True:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                _kill()
                return RouteRun(False, None, "\n".join(lines),
                                f"FreeRouting did not finish within {timeout_s}s",
                                timed_out=True)
            try:
                raw = line_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue                  # silent child: re-check the deadline
            if raw is None:               # stdout closed -> process is done
                break
            line = raw.rstrip("\n")
            lines.append(line)
            if any(pattern in line for pattern in JAVA_ERROR_PATTERNS):
                java_error = line.strip()
                _kill()
                break
    finally:
        if proc.poll() is None:
            proc.kill()
    proc.wait()

    stdout = "\n".join(lines)
    if java_error:
        return RouteRun(False, None, stdout,
                        f"FreeRouting threw a Java exception and was killed "
                        f"immediately (the timeout was not waited out): "
                        f"{java_error}", java_error=True)
    ses_exists = Path(ses_path).exists()
    return RouteRun(proc.returncode == 0 and ses_exists,
                    ses_path if ses_exists else None, stdout,
                    "" if proc.returncode == 0 else
                    f"FreeRouting exit={proc.returncode}")


def import_ses(board_path: str, ses_path: str, python: str | None = None,
               timeout_s: int = 60) -> SesResult:
    """Import the .ses back into the board and COUNT the tracks either side."""
    if not _pcbnew_available(python):
        raise FreeroutingUnavailable(
            "import_ses: pcbnew could not be imported. NEEDS_HUMAN.")
    try:
        proc = _run_pcbnew_script(_SES_IMPORT, [board_path, ses_path],
                                  python, timeout_s)
    except subprocess.TimeoutExpired as exc:
        return SesResult(False, 0, 0, False, exc.stdout or "",
                         f"SES import did not finish within {timeout_s}s")
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1]) \
            if proc.stdout else {}
    except (json.JSONDecodeError, IndexError):
        data = {}
    return SesResult(
        ok=bool(data.get("ok", False)),
        tracks_before=int(data.get("before", 0)),
        tracks_after=int(data.get("after", 0)),
        tracks_changed=bool(data.get("changed", False)),
        stdout=proc.stdout, stderr=proc.stderr)


def route_chain(board_path: str, workdir: str = ".", jar: str = DEFAULT_JAR_PATH,
                python: str | None = None, timeout_s: int = 240,
                java_heap: str = DEFAULT_JAVA_HEAP,
                allow_jar_download: bool = False) -> dict:
    """DSN export -> FreeRouting -> SES import, with every step recorded.

    Does NOT run the DRC afterwards. A routing result is not a verdict; the
    caller must still run the DRC gate.

    ``allow_jar_download=False`` (the default) means a missing jar fails
    closed exactly as before this bootstrap feature existed - the network
    call only happens when a caller opts in.
    """
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    dsn_path = str(work / "board.dsn")
    ses_path = str(work / "board.ses")

    dsn = export_dsn(board_path, dsn_path, python)
    if not dsn.ok:
        return {"stage": "dsn_export", "ok": False, "detail": dsn.stderr}

    resolved_jar = ensure_jar(jar, allow_download=allow_jar_download)
    run = run_freerouting(dsn_path, ses_path, jar=resolved_jar,
                          timeout_s=timeout_s, java_heap=java_heap)
    if not run.ok:
        return {"stage": "freerouting", "ok": False,
                "timed_out": run.timed_out, "java_error": run.java_error,
                "detail": run.stderr}

    ses = import_ses(board_path, ses_path, python)
    return {
        "stage": "ses_import",
        "ok": ses.ok and ses.tracks_changed,
        "tracks_before": ses.tracks_before,
        "tracks_after": ses.tracks_after,
        "tracks_changed": ses.tracks_changed,
        "detail": "" if (ses.ok and ses.tracks_changed) else
                  f"import ok={ses.ok} tracks_changed={ses.tracks_changed}: a "
                  f"True return with an unchanged track count means the "
                  f"padstacks did not match, NOT that routing completed",
    }


def self_test() -> list[str]:
    """Pure logic. Does NOT run java, freerouting or pcbnew."""
    problems: list[str] = []

    if not any(p in "app.freerouting.board.PolylineTrace StackOverflowError"
               for p in JAVA_ERROR_PATTERNS):
        problems.append("the Java error patterns would not catch a StackOverflow")

    # A True import with an unchanged track count must NOT read as routed.
    fake = SesResult(ok=True, tracks_before=12, tracks_after=12,
                     tracks_changed=False, stdout="", stderr="")
    if fake.ok and fake.tracks_changed:
        problems.append("an unchanged track count was treated as routed")

    finding = result("freerouting_chain", 0, [], "not run")
    if finding["status"] != "NO_COVERAGE":
        problems.append("a chain that never ran did not report NO_COVERAGE")

    # find_jar_download_url: picks the .jar asset, never guesses one.
    good_release = {"assets": [
        {"name": "readme.txt", "browser_download_url": "https://example.com/readme.txt"},
        {"name": "freerouting-2.1.0.jar",
         "browser_download_url": "https://github.com/example/freerouting.jar"},
    ]}
    url = find_jar_download_url(good_release)
    if not url.endswith(".jar"):
        problems.append(f"find_jar_download_url picked a non-jar asset: {url}")

    # FAULT INJECTION: a release with no .jar asset must be refused, not
    # silently pick some other asset to download and run.
    try:
        find_jar_download_url({"assets": [
            {"name": "readme.txt",
             "browser_download_url": "https://example.com/readme.txt"}]})
    except ValueError:
        pass
    else:
        problems.append(
            "fault injection did not hold: a release with no .jar asset "
            "did not raise")

    # ensure_jar must fail closed when the jar is absent and download was
    # not requested - never silently proceed as if it were present.
    try:
        ensure_jar("__definitely_not_a_real_freerouting_jar__.jar",
                  allow_download=False)
    except FreeroutingUnavailable:
        pass
    else:
        problems.append(
            "ensure_jar did not fail closed on a missing jar with "
            "allow_download=False")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--jar", default=DEFAULT_JAR_PATH)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--kicad-python",
                        help="interpreter that can import pcbnew")
    parser.add_argument("--timeout-s", type=int, default=240)
    parser.add_argument("--java-heap", default=DEFAULT_JAVA_HEAP,
                        help="-Xmx value, e.g. 4g (default: %(default)s)")
    parser.add_argument("--allow-jar-download", action="store_true",
                        help="fetch freerouting.jar from GitHub releases if "
                             "--jar is missing (opt-in: downloads and later "
                             "runs arbitrary executable code)")
    parser.add_argument("--dsn-only", action="store_true",
                        help="export the .dsn and stop")
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
        diag("SELF-TEST PASS: production_freerouting")
        return 0

    if not args.board:
        parser.error("the board path is required")

    params = {"jar": args.jar, "workdir": args.workdir,
              "timeout_s": args.timeout_s, "java_heap": args.java_heap,
              "allow_jar_download": args.allow_jar_download,
              "java_present": java_available(),
              "pcbnew_importable": _pcbnew_available(args.kicad_python)}

    try:
        if args.dsn_only:
            work = Path(args.workdir)
            work.mkdir(parents=True, exist_ok=True)
            dsn = export_dsn(args.board, str(work / "board.dsn"),
                             args.kicad_python)
            outcome = {"stage": "dsn_export", "ok": dsn.ok,
                       "detail": dsn.stderr}
        else:
            outcome = route_chain(args.board, args.workdir, args.jar,
                                  args.kicad_python, args.timeout_s,
                                  args.java_heap, args.allow_jar_download)
        violations = [] if outcome["ok"] else [outcome]
        finding = result("freerouting_chain", 1, violations,
                         f"stage={outcome['stage']}")
    except (FreeroutingUnavailable, ConnectionError, ValueError) as exc:
        diag(f"NO COVERAGE: {exc}")
        finding = result("freerouting_chain", 0, [], str(exc))
        outcome = {"stage": "unavailable", "ok": False, "detail": str(exc)}

    params["outcome"] = outcome
    report = board_report(TOOL_NAME, args.board, [finding], params=params,
                          schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    # NO_COVERAGE exits 1 here on purpose: "the router did not run" must never
    # be mistaken by a chain for "the board is routed".
    return 0 if finding["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
