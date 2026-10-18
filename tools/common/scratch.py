"""Scratch / canonical separation — the governance layer.

Ported from ``scratch_yonetimi.py``. No autonomous session writes DIRECTLY to
the canonical ``.kicad_pro``/``.kicad_sch``/``.kicad_pcb`` files: work happens
in a scratch COPY of the project directory, and the move back to canonical
(promotion) happens only once every gate (DRC/ERC plus the project-specific
contract) has passed. This makes "the tool fools itself and writes something
unclean to the live files" STRUCTURALLY impossible — without promotion the
canonical file never changes.

This module needs no ``pcbnew`` and no ``kicad-cli``: it is plain file
copying. Library only; no CLI, no stdout writes.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

SCRATCH_ROOT_NAME = ".scratch"

# Directories that must NEVER be carried into a scratch copy: git history,
# earlier scratches, editor/IDE state.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    SCRATCH_ROOT_NAME, ".git", "__pycache__", ".obsidian", ".vscode",
    ".pytest_cache",
)


def new_scratch_id() -> str:
    """A sortable, low-collision id: a UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def scratch_root(project_dir: str) -> Path:
    return Path(project_dir).resolve() / SCRATCH_ROOT_NAME


def create_scratch(project_dir: str, scratch_id: str | None = None,
                   excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> Path:
    """Copy ``project_dir`` into ``<project_dir>/.scratch/<scratch_id>/`` and
    return the path of the new copy.

    During this call the canonical files are READ ONLY — nothing canonical is
    written or deleted (the copy is one-way: canonical -> scratch).
    """
    source = Path(project_dir).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"proje dizini yok: {source}")
    sid = scratch_id or new_scratch_id()
    target = scratch_root(project_dir) / sid
    if target.exists():
        raise FileExistsError(
            f"scratch zaten var: {target} — farklı bir scratch_id kullan")
    target.parent.mkdir(parents=True, exist_ok=True)

    def ignore(directory: str, names: list) -> list:
        return [n for n in names if n in excludes]

    shutil.copytree(source, target, ignore=ignore)
    return target


def assert_within_scratch(scratch_path: Path, project_dir: str) -> None:
    """SECURITY CONTROL: prove that an operation really runs inside the
    scratch tree and has not escaped to the canonical directory.

    Raises ``ValueError`` when ``scratch_path`` is not under
    ``project_dir/.scratch/``. This is ``promote_to_canonical``'s fail-closed
    precondition and is never skipped.
    """
    root = scratch_root(project_dir)
    try:
        Path(scratch_path).resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"{scratch_path} scratch dizini ({root}) içinde DEĞİL — "
            "kanonik dosyaya kazara yazma riski nedeniyle işlem durduruldu."
        ) from exc


def promote_to_canonical(
    scratch_path: Path,
    project_dir: str,
    excludes: tuple[str, ...] = (SCRATCH_ROOT_NAME,),
) -> None:
    """Copy files from the scratch tree to the canonical ``project_dir``.

    THIS FUNCTION RUNS NO GATE OF ITS OWN — it only verifies that
    ``scratch_path`` really is inside the scratch tree
    (``assert_within_scratch``) and then copies. Verifying that the DRC/ERC
    and project-contract gates PASSED is the caller's job; this is only the
    "the gates already passed, now copy" step.
    """
    assert_within_scratch(scratch_path, project_dir)
    target = Path(project_dir).resolve()
    source = Path(scratch_path).resolve()
    for entry in source.iterdir():
        if entry.name in excludes:
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)


def list_scratches(project_dir: str) -> list[str]:
    """Existing scratch ids, newest first — for offering the user a choice."""
    root = scratch_root(project_dir)
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)
