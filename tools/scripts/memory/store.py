"""Where the memory files live, and the vault rule that keeps them out.

STORAGE DECISION
----------------
The archived ``hata_hafizasi.py`` defaulted to a project-relative
``HAFIZA/Hata_Hafizasi.md`` and documented an Obsidian vault as the intended
home ("Obsidian uyumlu bir Markdown veritabani"); the recovery engine wrote
its notes next to it. This project has a standing rule that the vault holds
NO design or tool state, so both stores are re-pointed under the pcb-agent
tree:

    pcb-agent/var/memory/error_memory.md
    pcb-agent/var/memory/recovery/

``var/`` is used rather than ``tools/`` because this is mutable run state,
not a versioned tool: it changes on every session, and mixing it into
``tools/`` would make the hash-bound tool registry churn for reasons that
have nothing to do with the tools.

Resolution order: ``--store`` > ``$PCB_AGENT_MEMORY_DIR`` > the default above.

THE VAULT GUARD IS FAIL-CLOSED
------------------------------
``assert_not_vault`` refuses any target inside an Obsidian vault, detected by
a ``.obsidian`` directory in the path or in any ancestor. It refuses BEFORE
creating anything. A configuration mistake therefore produces an error, not a
quiet write into someone's notes - which is exactly the failure the rule
exists to prevent, and the archive's default made easy.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "PCB_AGENT_MEMORY_DIR"
DEFAULT_SUBPATH = ("var", "memory")
ERROR_MEMORY_FILE = "error_memory.md"
RECOVERY_DIR = "recovery"
JLC_PARTS_CACHE_DIR = "jlc_parts_cache"

#: A directory with this name anywhere in the path means an Obsidian vault.
VAULT_MARKERS = (".obsidian", ".obsidian.vimrc")


class VaultWriteRefused(RuntimeError):
    """The requested store is inside an Obsidian vault."""


def repo_root() -> Path:
    """``pcb-agent/`` - the parent of ``tools/``."""
    return Path(__file__).resolve().parents[3]


def default_store() -> Path:
    return repo_root().joinpath(*DEFAULT_SUBPATH)


def assert_not_vault(path: Path) -> Path:
    """Refuse a store inside an Obsidian vault. Checked before any mkdir."""
    path = Path(path).expanduser()
    absolute = Path(os.path.abspath(path))
    if any(part in VAULT_MARKERS for part in absolute.parts):
        raise VaultWriteRefused(
            f"{absolute} is inside an Obsidian vault; the vault holds no "
            "design or tool state. Use --store or $PCB_AGENT_MEMORY_DIR.")
    for ancestor in (absolute, *absolute.parents):
        for marker in VAULT_MARKERS:
            if (ancestor / marker).exists():
                raise VaultWriteRefused(
                    f"{absolute} is inside the Obsidian vault rooted at "
                    f"{ancestor}; the vault holds no design or tool state. "
                    "Use --store or $PCB_AGENT_MEMORY_DIR.")
    return absolute


def resolve_store(explicit: str | Path | None = None, *,
                  create: bool = False) -> Path:
    raw = explicit or os.environ.get(ENV_VAR) or default_store()
    store = assert_not_vault(Path(raw))
    if create:
        store.mkdir(parents=True, exist_ok=True)
    return store


def error_memory_path(explicit: str | Path | None = None, *,
                      create: bool = False) -> Path:
    return resolve_store(explicit, create=create) / ERROR_MEMORY_FILE


def recovery_dir(explicit: str | Path | None = None, *,
                 create: bool = False) -> Path:
    directory = resolve_store(explicit, create=create) / RECOVERY_DIR
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def jlc_parts_cache_dir(explicit: str | Path | None = None, *,
                        create: bool = False) -> Path:
    """Where downloaded LCSC symbol/footprint/3D-model bundles are cached,
    keyed by LCSC code - same vault-guarded ``var/memory/`` tree as
    error-memory and recovery, for the same reason: mutable run state, not a
    versioned tool."""
    directory = resolve_store(explicit, create=create) / JLC_PARTS_CACHE_DIR
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    return directory
