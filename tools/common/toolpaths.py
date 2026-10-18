"""Resolution of the external EDA tools that are NOT kicad-cli.

Ported from ``arac_yollari.py``, minus the kicad-cli resolver: that one is
already merged into ``common/kicad_cli.py`` and is CALLED from here, never
re-implemented. What is left is:

  * KiCad's EMBEDDED Python interpreter (the only interpreter that has
    ``pcbnew``),
  * poppler's ``pdftocairo`` (PDF -> PNG for the visual cross-section step),
  * the "check every tool" health matrix.

Library only: no CLI, no stdout writes. stdlib + subprocess only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .kicad_cli import KICAD_CLI_ENV, KicadCliError, _require_cli

KICAD_PYTHON_ENV = "KICAD_PYTHON"
POPPLER_BIN_ENV = "POPPLER_BIN"

__all__ = [
    "KICAD_PYTHON_ENV", "POPPLER_BIN_ENV", "KICAD_CLI_ENV",
    "ToolStatus", "find_kicad_python", "find_pdftocairo",
    "run_pcbnew_script", "kicad_cli_version", "preflight", "check_all_tools",
]


# --------------------------------------------------------------------------- #
# KiCad's embedded Python (where pcbnew lives)
# --------------------------------------------------------------------------- #
def _windows_kicad_python_candidates() -> list[Path]:
    """The ``python.exe`` sitting NEXT TO ``kicad-cli.exe``.

    Throughout this project the real ``pcbnew`` (SWIG) exists ONLY inside
    KiCad's own Python distribution: ``import pcbnew`` in a project venv
    ALWAYS raises ``ModuleNotFoundError``, and that is not an environment
    defect — ``pcbnew`` is simply not distributed as a PyPI package. Same
    root-directory scan as the kicad-cli resolver: every version directory
    that has ``bin/kicad-cli.exe`` also has ``bin/python.exe``.
    """
    if os.name != "nt":
        return []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    root = Path(program_files) / "KiCad"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/bin/python.exe"), reverse=True)


def _posix_kicad_python_candidates() -> list[Path]:
    """macOS ve Linux adayları.

    İKİ İŞLETİM SİSTEMİ İKİ FARKLI MODEL — ve fark önemlidir:

    * **macOS**: KiCad kendi Python.framework'ünü taşır, tıpkı Windows gibi.
      Aday, uygulama paketinin içindeki yorumlayıcıdır.
    * **Linux**: KiCad genelde `pcbnew`'i **SİSTEM** python'unun
      `dist-packages`'ına kurar; ayrı bir gömülü yorumlayıcı YOKTUR. Orada
      doğru cevap çoğu zaman "zaten koştuğun python" olur. Bu yüzden mevcut
      yorumlayıcı da bir adaydır — ama `verify=True` ile GERÇEKTEN
      `import pcbnew` yapabildiği sınanmadan seçilmez.
    """
    if os.name == "nt":
        return []
    out: list[Path] = []
    # macOS: uygulama paketi içindeki framework
    for pat in ("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                "Python.framework/Versions/*/bin/python3",
                "/Applications/KiCad.app/Contents/Frameworks/"
                "Python.framework/Versions/*/bin/python3"):
        out += sorted(Path("/").glob(pat.lstrip("/")), reverse=True)
    # Linux: sistem python'u (pcbnew dist-packages'ta) + flatpak
    out.append(Path(sys.executable))
    for p in ("/usr/bin/python3", "/usr/lib/kicad/bin/python3",
              "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/bin/python3"):
        out.append(Path(p))
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


def _kicad_python_candidates() -> list[Path]:
    """İşletim sistemine göre aday yorumlayıcılar (Windows / macOS / Linux)."""
    return _windows_kicad_python_candidates() + _posix_kicad_python_candidates()


def imports_pcbnew(interpreter: str | Path, timeout_s: int = 25) -> str | None:
    """`interpreter` GERÇEKTEN `pcbnew` import edebiliyor mu? -> sürüm ya da None.

    Dosyanın var olması yetmez. Windows'ta `python.exe`'yi bulmak kolaydır ama
    yanlış olanı seçmek sessizdir; ayrıca `_pcbnew.pyd` belirli bir CPython
    ABI'sine derlenmiştir (ör. `.cp311-win_amd64.pyd`) ve başka bir minor
    sürümde `Module use of python311.dll conflicts with this version of Python`
    ile düşer. Tek dürüst sınama, çağırıp sormaktır.
    """
    try:
        r = subprocess.run(
            [str(interpreter), "-c",
             "import pcbnew; print(pcbnew.GetBuildVersion())"],
            capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def find_kicad_python(requested: str | None = None, verify: bool = False) -> str:
    """Resolve the embedded KiCad interpreter that contains ``pcbnew``.

    Same priority discipline as the kicad-cli resolver: explicit argument ->
    ``$KICAD_PYTHON`` -> the standard Windows install directory. EVERY script
    that needs ``pcbnew`` must be run with the interpreter returned here; the
    project venv's own ``python`` can never see ``pcbnew``.

    NOTE: PATH is deliberately NOT searched. A bare ``python`` on PATH is
    almost certainly the wrong interpreter, and silently returning it would
    produce a confusing ``ModuleNotFoundError`` far from here.

    ``verify=True`` her adayı çağırıp ``import pcbnew`` yapabildiğini SINAR ve
    ilk gerçekten çalışanı döner. Varsayılan ``False``, eski çağıranların
    davranışını korumak içindir (bulunan ilk dosya). Yeni kod ``verify=True``
    kullanmalı: "python.exe var" ile "pcbnew import ediliyor" aynı şey değildir
    (bkz. ``imports_pcbnew`` baş notu).
    """
    if requested:
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            if verify and imports_pcbnew(candidate) is None:
                raise FileNotFoundError(
                    f"İstenen yorumlayıcı 'pcbnew' import edemiyor: {candidate}")
            return str(candidate)
        raise FileNotFoundError(f"İstenen KiCad Python bulunamadı: {requested}")

    from_env = os.environ.get(KICAD_PYTHON_ENV)
    if from_env:
        return find_kicad_python(from_env, verify=verify)

    tried: list[str] = []
    for candidate in _kicad_python_candidates():
        if not candidate.is_file():
            continue
        if not verify:
            return str(candidate)
        if imports_pcbnew(candidate) is not None:
            return str(candidate)
        tried.append(str(candidate))

    raise FileNotFoundError(
        "KiCad'in gömülü Python yorumlayıcısı bulunamadı (pcbnew burada "
        f"yaşar; işletim sistemi: {sys.platform}). ${KICAD_PYTHON_ENV} ortam "
        "değişkenini '<KiCad kurulum dizini>/bin/python.exe' (Windows), "
        "'/Applications/KiCad/KiCad.app/.../bin/python3' (macOS) ya da "
        "pcbnew'i taşıyan sistem python'u (Linux) yoluna ayarla."
        + (f"\n  Denenip pcbnew import edemeyenler: {tried}" if tried else ""))


def run_pcbnew_script(script_path: str, argv: list[str] | None = None,
                      kicad_python: str | None = None,
                      timeout_s: int = 120) -> subprocess.CompletedProcess:
    """Run a ``.py`` file with KiCad's embedded (pcbnew-carrying) Python, so
    that the rest of the project can stay on its own interpreter — the
    ``pcbnew`` import is needed ONLY in this subprocess."""
    interpreter = find_kicad_python(kicad_python)
    return subprocess.run([interpreter, script_path] + (argv or []),
                          capture_output=True, text=True, timeout=timeout_s)


# --------------------------------------------------------------------------- #
# poppler / pdftocairo
# --------------------------------------------------------------------------- #
def _windows_poppler_candidates() -> list[Path]:
    """Where winget puts poppler (``pdftocairo``) on Windows.

    scoop/choco installs land on PATH already, so ONLY winget's
    ``WinGet\\Packages\\...`` layout is scanned here.
    """
    if os.name != "nt":
        return []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return []
    root = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
    if not root.is_dir():
        return []
    return sorted(root.glob("*poppler*/**/pdftocairo.exe"), reverse=True)


def find_pdftocairo(requested: str | None = None) -> str:
    """Resolve ``pdftocairo`` (poppler) with the same priority discipline as
    the kicad-cli resolver: argument > ``$POPPLER_BIN`` > PATH > the Windows
    winget install directory."""
    if requested:
        candidate = Path(requested).expanduser()
        if candidate.is_file():
            return str(candidate)
        on_path = shutil.which(requested)
        if on_path:
            return on_path
        raise FileNotFoundError(f"İstenen pdftocairo bulunamadı: {requested}")

    from_env = os.environ.get(POPPLER_BIN_ENV)
    if from_env:
        return find_pdftocairo(from_env)

    on_path = shutil.which("pdftocairo")
    if on_path:
        return on_path

    for candidate in _windows_poppler_candidates():
        if candidate.is_file():
            return str(candidate)

    raise FileNotFoundError(
        "pdftocairo (poppler) bulunamadı. Kurulum: `winget install "
        "oschwartz10612.Poppler` (Windows) veya `apt install poppler-utils` "
        f"(Linux) / `brew install poppler` (macOS); ya da ${POPPLER_BIN_ENV} "
        "ortam değişkenini pdftocairo'nun tam yoluna ayarla."
    )


# --------------------------------------------------------------------------- #
# Health matrix
# --------------------------------------------------------------------------- #
def kicad_cli_version(kicad_cli: str | None = None) -> tuple[str, str]:
    """Resolve kicad-cli via ``common.kicad_cli`` and prove it RUNS.

    Resolution is not duplicated here: ``kicad_cli._require_cli`` is the one
    implementation (argument -> ``$KICAD_CLI`` -> PATH -> Windows install).
    The only thing added is the ``--version`` round-trip, because "the file
    exists" is not the same claim as "the tool works".
    """
    path = _require_cli(kicad_cli)
    completed = subprocess.run([path, "--version"], capture_output=True,
                               text=True, timeout=15)
    text = (completed.stdout or completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(f"KiCad CLI sürümü okunamadı ({path}): {text}")
    return path, text


def preflight(kicad_cli: str | None = None) -> list[str]:
    """Portable, fail-closed preflight for kicad-cli alone."""
    path, version = kicad_cli_version(kicad_cli)
    return [f"PASS KiCad CLI: {path}", f"PASS sürüm: {version}",
            f"PASS Python: {sys.version.split()[0]}"]


@dataclass
class ToolStatus:
    """The preflight result for one external tool, carrying the install hint
    it needs, so that a missing tool produces a real instruction rather than
    an assumption that it is installed."""

    name: str
    ok: bool
    detail: str
    install_hint: str

    def line(self) -> str:
        state = "PASS" if self.ok else "FAIL"
        if self.ok:
            return f"{state} {self.name}: {self.detail}"
        return f"{state} {self.name}: {self.detail} (bkz. {self.install_hint})"


def _command_status(name: str, command: list[str], install_hint: str,
                    timeout_s: int = 20) -> ToolStatus:
    """Call a CLI with ``--version`` (or similar) and produce PASS/FAIL.

    Never raises: a tool missing from PATH, a timeout, a permission error all
    return ``ToolStatus(ok=False, ...)`` so that ONE missing tool cannot cut
    the whole preflight short.
    """
    path = shutil.which(command[0])
    if path is None:
        return ToolStatus(name, False,
                          f"'{command[0]}' bulunamadı (PATH'te değil)",
                          install_hint)
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(name, False, f"çalıştırılamadı: {exc}", install_hint)
    text = ((completed.stdout or "") + (completed.stderr or "")).strip()
    first_line = text.splitlines()[0] if text else ""
    return ToolStatus(name, completed.returncode == 0,
                      first_line or f"exit={completed.returncode}", install_hint)


def _python_import_status(name: str, import_expr: str,
                          install_hint: str) -> ToolStatus:
    """``python3 -c "import X"`` with the CURRENT interpreter."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {import_expr}"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus(name, False, f"çalıştırılamadı: {exc}", install_hint)
    if completed.returncode == 0:
        return ToolStatus(name, True, "import OK", install_hint)
    stderr_lines = (completed.stderr or "").strip().splitlines()
    return ToolStatus(name, False,
                      stderr_lines[-1] if stderr_lines else "import başarısız",
                      install_hint)


def _pcbnew_status() -> ToolStatus:
    """Check ``pcbnew`` with KiCad's embedded interpreter, NOT
    ``sys.executable``.

    FIXED BUG (kept from the source): this used to go through
    ``_python_import_status`` with ``sys.executable``. Because ``pcbnew`` is
    not a PyPI package, that reported FAIL even when KiCad was fully
    installed and ``pcbnew`` genuinely usable — and ``kipy`` was folded into
    the same import, so one missing package masked the other's PASS. ``kipy``
    is now reported separately and optionally; this function looks at
    ``pcbnew`` ONLY.
    """
    hint = "KiCad kurulumu (kicad-cli + gömülü python)"
    try:
        interpreter = find_kicad_python()
    except FileNotFoundError as exc:
        return ToolStatus("pcbnew (gerçek-board kontrolleri)", False, str(exc),
                          hint)
    try:
        completed = subprocess.run(
            [interpreter, "-c", "import pcbnew; print(pcbnew.GetBuildVersion())"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolStatus("pcbnew (gerçek-board kontrolleri)", False,
                          f"çalıştırılamadı: {exc}", hint)
    if completed.returncode == 0:
        return ToolStatus("pcbnew (gerçek-board kontrolleri)", True,
                          f"{interpreter} ({completed.stdout.strip()})", hint)
    stderr_lines = (completed.stderr or "").strip().splitlines()
    return ToolStatus("pcbnew (gerçek-board kontrolleri)", False,
                      stderr_lines[-1] if stderr_lines else "import başarısız",
                      hint)


def _kipy_status() -> ToolStatus:
    """``kipy`` (the live KiCad IPC API, ``pip install kicad-python``) is
    installed into the project venv — UNLIKE ``pcbnew`` it really is a PyPI
    package, so checking it with ``sys.executable`` is CORRECT. The two are
    kept apart deliberately (see ``_pcbnew_status``)."""
    return _python_import_status("kipy (canlı KiCad IPC, opsiyonel)", "kipy",
                                 "pip install kicad-python")


def check_all_tools(kicad_cli: str | None = None) -> list[ToolStatus]:
    """Check every external tool and return a ``ToolStatus`` list.

    DELIBERATELY DIFFERENT from a bash ``A && B && C`` chain: there the first
    failing command CUTS the chain and the later tools are never checked, so
    the user learns only the FIRST missing tool, not WHICH tools are missing.
    Here every tool is tried independently — one missing tool never prevents
    the others from being checked, and a single call yields the whole table.
    That also side-steps PowerShell 5.1's lack of ``&&``: no shell chaining
    is needed at all, one Python call is enough.
    """
    statuses: list[ToolStatus] = []

    try:
        path, version = kicad_cli_version(kicad_cli)
        statuses.append(ToolStatus("KiCad CLI", True, f"{path} ({version})",
                                   f"KiCad kurulumu veya ${KICAD_CLI_ENV}"))
    except (KicadCliError, FileNotFoundError, RuntimeError,
            subprocess.SubprocessError, OSError) as exc:
        statuses.append(ToolStatus("KiCad CLI", False, str(exc),
                                   f"KiCad kurulumu veya ${KICAD_CLI_ENV}"))

    statuses.append(_pcbnew_status())
    statuses.append(_kipy_status())
    statuses.append(_command_status("Node.js", ["node", "--version"],
                                    "nodejs kurulumu"))
    statuses.append(_command_status("Java", ["java", "-version"],
                                    "JRE kurulumu (freerouting için)"))
    statuses.append(_python_import_status("JLC2KiCadLib", "JLC2KiCadLib",
                                          "pip install JLC2KiCadLib"))
    statuses.append(_command_status("KiBot", ["kibot", "--version"],
                                    "pip install kibot"))
    statuses.append(_command_status("uv", ["uv", "--version"], "uv kurulumu"))
    statuses.append(_command_status("git", ["git", "--version"],
                                    "git kurulumu"))

    try:
        statuses.append(ToolStatus("pdftocairo (poppler)", True,
                                   find_pdftocairo(),
                                   "poppler-utils kurulumu"))
    except FileNotFoundError as exc:
        statuses.append(ToolStatus("pdftocairo (poppler)", False, str(exc),
                                   "poppler-utils kurulumu"))
    statuses.append(_python_import_status("svglib/reportlab",
                                          "svglib, reportlab",
                                          "pip install svglib reportlab"))

    return statuses
