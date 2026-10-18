"""kicad-cli bridge: netlist truth, ERC, and the "is KiCad open?" guard.

Hoisted from sch_wire so that board-side tools can reuse the same gate
contract instead of re-shelling kicad-cli with different flags.

GATE CONTRACT: without ``--exit-code-violations`` kicad-cli reports violations
in the JSON but still exits 0, so a gate that only checks the exit code gives
a false PASS. Every gate helper here passes it.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .sexpr import find, find_all, parse_sexpr, unquote

# Process names that mean "a KiCad GUI may be holding this file".
# kicad-cli is deliberately absent: it is headless and short-lived, and it is
# what these tools invoke themselves.
KICAD_GUI_PROCESSES = (
    "kicad",
    "pcbnew",
    "eeschema",
    "kicad-nightly",
    "pcbnew-nightly",
    "eeschema-nightly",
)


class KicadCliError(RuntimeError):
    """kicad-cli was missing or failed."""


KICAD_CLI_ENV = "KICAD_CLI"


def _windows_kicad_cli_candidates() -> list:
    """Newest-first ``kicad-cli.exe`` under the standard Windows install dir."""
    if os.name != "nt":
        return []
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    root = Path(program_files) / "KiCad"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/bin/kicad-cli.exe"), reverse=True)


def _require_cli(wanted: str | None = None) -> str:
    """Resolve kicad-cli: explicit arg -> $KICAD_CLI -> PATH -> Windows install.

    PATH alone is not enough: the Windows installer does not put kicad-cli on
    PATH, and a machine with several KiCad versions needs an explicit override.
    Never silently returns the bare string "kicad-cli" — an unresolvable tool
    must fail here, not later inside a gate with a confusing error.
    """
    if wanted:
        candidate = Path(wanted).expanduser()
        if candidate.is_file():
            return str(candidate)
        on_path = shutil.which(wanted)
        if on_path:
            return on_path
        raise KicadCliError(f"requested kicad-cli not found: {wanted}")

    from_env = os.environ.get(KICAD_CLI_ENV)
    if from_env:
        return _require_cli(from_env)

    exe = shutil.which("kicad-cli")
    if exe is not None:
        return exe

    for candidate in _windows_kicad_cli_candidates():
        if candidate.is_file():
            return str(candidate)

    raise KicadCliError(
        f"kicad-cli not found. Install KiCad, set ${KICAD_CLI_ENV} to the full "
        "path of kicad-cli, or put it on PATH.")


def running_kicad_processes(names=KICAD_GUI_PROCESSES) -> list[str]:
    """Return the ``pgrep -a`` lines for any running KiCad GUI process.

    Queries each name SEPARATELY rather than passing an alternation pattern.

    Why: ``pgrep -a -x "kicad|pcbnew|eeschema"`` does work on procps-ng
    (``-x`` anchors the ERE, so the alternation is honoured - verified against
    procps-ng 4.0.6), but ``-x`` is specified as an *exact string* match, and
    on the BSD/macOS pgrep it is implemented that way: the alternation is then
    matched literally and the guard silently never fires. A per-name loop has
    identical semantics everywhere and costs nothing.

    A missing pgrep is reported as "cannot determine", not as "nothing
    running", so the caller can fail closed.

    WINDOWS: pgrep does not exist there, so an unconditional pgrep call raised
    an uncaught FileNotFoundError and aborted the whole write. Windows uses
    ``tasklist`` instead. Note the matching is necessarily weaker there
    (substring over the image name), which can only produce false POSITIVES —
    a spurious refusal to write, never a silent overwrite.
    """
    if platform.system() == "Windows":
        try:
            proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise KicadCliError(
                f"tasklist failed; cannot verify that KiCad is closed: {exc}")
        live = []
        for line in proc.stdout.splitlines():
            lowered = line.lower()
            if any(f'"{name}.exe"' in lowered for name in names):
                live.append(line.strip())
        return live

    if shutil.which("pgrep") is None:
        raise KicadCliError(
            "pgrep not found; cannot verify that KiCad is closed. "
            "Close KiCad and pass force=True if you are certain.")
    live: list[str] = []
    for name in names:
        proc = subprocess.run(["pgrep", "-a", "-x", name],
                              capture_output=True, text=True)
        # rc 0 = matches, 1 = no match, >1 = pgrep error
        if proc.returncode > 1:
            raise KicadCliError(
                f"pgrep failed for {name!r}: {proc.stderr.strip()}")
        live.extend(line for line in proc.stdout.splitlines() if line.strip())
    return live


def assert_kicad_closed(path: str, *, force: bool = False) -> None:
    """Refuse to write a file that an open KiCad may overwrite.

    An open KiCad and a file writer silently clobber each other; this is the
    single most destructive failure mode in the bundle.
    """
    if force:
        return
    lock = os.path.join(os.path.dirname(os.path.abspath(path)),
                        "~" + os.path.basename(path) + ".lck")
    if os.path.exists(lock) or os.path.exists(path + ".lck"):
        raise RuntimeError(f"Kilit dosyası var, KiCad açık görünüyor: {path}")
    live = running_kicad_processes()
    if live:
        raise RuntimeError(
            "KiCad çalışıyor — yazmadan önce kapat:\n" + "\n".join(live))


def netlist_nets(sch: str, workdir: str | None = None) -> dict[str, set[str]]:
    """Export a netlist with kicad-cli and return {net: {'R1-1', ...}}.

    THIS IS THE ONLY PROOF OF CONNECTIVITY. A wire being visible in the
    schematic does not mean the pins are connected.
    """
    exe = _require_cli()
    out = (workdir or os.path.dirname(os.path.abspath(sch))) + "/_netlist.net"
    proc = subprocess.run([exe, "sch", "export", "netlist",
                           "--format", "kicadsexpr", "-o", out, sch],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise KicadCliError(
            f"netlist export başarısız:\n{proc.stdout}\n{proc.stderr}")
    tree = parse_sexpr(Path(out).read_text(encoding="utf-8"))
    nets: dict[str, set[str]] = {}
    nets_node = find(tree, "nets")
    for net in find_all(nets_node or [], "net"):
        name_node = find(net, "name")
        name = unquote(name_node[1]) if name_node else "?"
        members = set()
        for node in find_all(net, "node"):
            ref = unquote(find(node, "ref")[1])
            pin = unquote(find(node, "pin")[1])
            members.add(f"{ref}-{pin}")
        nets[name] = members
    return nets


def verify_nets(sch: str, expected: dict[str, set[str]]) -> list[str]:
    """Compare the real netlist against ``expected``. Empty list means pass.

    Two traps are normalised here:
      * locally-labelled nets appear with a sheet-path prefix (``/VBUS``);
        global labels and power-symbol names are unprefixed.
      * ``#PWR*`` power symbols do NOT appear as nodes; the proof that a power
        symbol is connected is that the net carries its name (e.g. ``GND``).
    """
    actual = {k.lstrip("/"): v for k, v in netlist_nets(sch).items()}
    problems: list[str] = []
    for name, want in expected.items():
        want = {m for m in want if not m.startswith("#")}
        got = actual.get(name.lstrip("/"))
        if got is None:
            problems.append(f"NET YOK: {name} (wire pin'e değmiyor olabilir)")
        elif not want <= got:
            problems.append(
                f"EKSİK ÜYE {name}: bekleniyor {sorted(want)}, gelen {sorted(got)}")
    return problems


def run_erc(sch: str, out: str | None = None,
            *, exit_code_violations: bool = False,
            severity_all: bool = False) -> dict:
    """Run ERC and return the parsed JSON. Silencing errors with PWR_FLAG is
    forbidden.

    ``exit_code_violations`` is off by default because the historical callers
    inspect the JSON themselves; gate callers must pass True.
    """
    exe = _require_cli()
    out = out or os.path.splitext(sch)[0] + "_erc.json"
    argv = [exe, "sch", "erc", "--format", "json"]
    if severity_all:
        argv.append("--severity-all")
    else:
        argv += ["--severity-error", "--severity-warning"]
    if exit_code_violations:
        argv.append("--exit-code-violations")
    argv += ["-o", out, sch]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if exit_code_violations and proc.returncode not in (0, 5):
        # 5 is the documented "violations found" code; anything else is a tool
        # failure and must not be read as a clean ERC.
        if not Path(out).is_file():
            raise KicadCliError(
                f"ERC failed:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(Path(out).read_text(encoding="utf-8"))


def erc_violations(report: dict) -> list[dict]:
    """Flatten a kicad-cli ERC JSON report into a list of violations."""
    return [
        issue
        for sheet in report.get("sheets", [])
        for issue in sheet.get("violations", [])
    ]


# --------------------------------------------------------------------------- #
# Folded in from the archived kicad_koprusu.py
#
# Only the pieces that did not already exist here were taken: the .kicad_pro
# netclass writer and the PCB SVG export. DRC/ERC running and violation
# flattening were already covered by run_erc/erc_violations above and by
# common/drc_summary.py, so they were NOT copied - duplicating a gate is how
# two callers end up disagreeing about what "clean" means.
#
# The archived module's `kipy` / IPC block was DROPPED. Its own docstring
# recorded that those calls were never executed even once, and KiCad 9/10
# cannot load or export headless over IPC at all, so shipping it would have
# been untested code wearing an API's name.
# --------------------------------------------------------------------------- #

def netclass_entry(name: str, track_width_mm: float, diff_pair_gap_mm: float,
                   nets: list[str]) -> dict:
    """One ``.kicad_pro`` net-class record.

    The key names follow KiCad 10's project schema. ``name`` is sanitised
    because KiCad rejects a net-class name containing separators.
    """
    safe = (name.replace(" ", "_").replace("/", "_")
                .replace("+", "P").replace("-", "N"))
    return {
        "name": safe,
        "track_width": track_width_mm,
        "diff_pair_gap": diff_pair_gap_mm,
        "nets": list(nets),
    }


def write_netclasses(kicad_pro_path: str, new_classes: list[dict], *,
                     force: bool = False) -> str:
    """Merge net classes into a ``.kicad_pro``, keeping a ``.bak``.

    Refuses to write while KiCad may be open (``assert_kicad_closed``): the
    GUI rewrites the project file on save and would silently clobber this.
    The archived version had no such guard.

    Returns the backup path.
    """
    project = Path(kicad_pro_path)
    if not project.is_file():
        raise FileNotFoundError(f"{kicad_pro_path} not found")
    assert_kicad_closed(str(project), force=force)

    backup = project.with_suffix(project.suffix + ".bak")
    backup.write_bytes(project.read_bytes())

    data = json.loads(project.read_text(encoding="utf-8"))
    netclasses = (data.setdefault("board", {})
                      .setdefault("design_settings", {})
                      .setdefault("net_classes", []))
    existing = {nc.get("name") for nc in netclasses}
    for entry in new_classes:
        if entry["name"] in existing:
            netclasses[:] = [entry if nc.get("name") == entry["name"] else nc
                             for nc in netclasses]
        else:
            netclasses.append(entry)

    project.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")
    return str(backup)


def export_pcb_svg(board: str, out: str,
                   layers: str = "F.Cu,B.Cu,F.SilkS,Edge.Cuts",
                   *, fit_page_to_board: bool = False,
                   single_page: bool = False,
                   cli: str | None = None) -> str:
    """``kicad-cli pcb export svg``. Returns the output path.

    ``fit_page_to_board`` + ``single_page`` are what a cropping consumer
    needs (page origin at the Edge.Cuts bbox corner, 1 unit = 1 mm); the
    plain call keeps KiCad's default page.
    """
    exe = _require_cli(cli)
    argv = [exe, "pcb", "export", "svg", "--layers", layers, "-o", out]
    if fit_page_to_board:
        argv += ["--page-size-mode", "2", "--fit-page-to-board"]
    if single_page:
        argv.append("--mode-single")
    argv.append(board)
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0 or not Path(out).exists():
        raise KicadCliError(
            f"pcb export svg failed:\n{proc.stdout}\n{proc.stderr}")
    return out


def export_pcb_step(board: str, out: str, *, subst_models: bool = True,
                    timeout_s: int = 180, cli: str | None = None):
    """``kicad-cli pcb export step``. Returns ``(path, stderr)``.

    ``subst_models`` keeps the export going when a single 3-D model is
    missing. The stderr is returned rather than swallowed BECAUSE of that:
    every substituted model is a component the downstream 3-D collision
    check is blind to, and the caller must report those.
    """
    exe = _require_cli(cli)
    argv = [exe, "pcb", "export", "step", "--force", "--output", out]
    if subst_models:
        argv.append("--subst-models")
    argv.append(board)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    if proc.returncode != 0:
        raise KicadCliError(f"pcb export step failed: {proc.stderr}")
    if not Path(out).exists():
        raise KicadCliError(f"{out} was not created; kicad-cli said: {proc.stdout}")
    return out, proc.stderr


def missing_3d_models(stderr: str) -> list[str]:
    """Refdes list from ``Could not add 3D model for X.`` lines."""
    import re
    return [m.group(1) for m in
            re.finditer(r"Could not add 3D model for (\S+?)\.?\s*$",
                        stderr, re.MULTILINE)]
