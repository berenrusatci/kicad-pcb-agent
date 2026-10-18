"""Deterministic UUID generation.

Hoisted from wireify._det_uuid and rewire._u, which differed only in the
namespace string prefix.

WHY THE KEY IS A PATH AND NOT A BASENAME (fixed 2026-08-06)
------------------------------------------------------------
The first version keyed on ``os.path.basename(path)``. Two DIFFERENT
schematics that merely share a filename then produced the SAME UUID, and
KiCad treats a duplicate UUID as the same object. That was not theoretical:
this project's own tree carries ``mipi.kicad_sch``, ``power.kicad_sch``,
``PCIe.kicad_sch``, ``hdmi.kicad_sch``, ``RTC.kicad_sch``, ``PSUs.kicad_sch``
and more, each under several different boards.

The full path is not the answer either. Every autonomous run works in a
``.scratch/<timestamp>/`` COPY that is promoted back to canonical
(``common/scratch.py``), so a full-path key would mint fresh UUIDs on every
single run and churn the whole file at each promotion.

So the key is the SCRATCH-INDEPENDENT path: the path with any
``.scratch/<id>/`` segment removed. A file keeps its identity as it moves
between its scratch copy and its canonical location, while two same-named
files under different boards stay distinct.
"""

from __future__ import annotations

import uuid as _uuid
from pathlib import PurePath

#: Mirrors ``common.scratch.SCRATCH_ROOT_NAME``. Duplicated deliberately:
#: this module is imported by the schematic writers and must stay free of
#: heavier imports, and the constant is a directory-naming convention that
#: cannot change without a migration anyway. ``tests/test_common.py`` pins
#: the two together so they cannot drift apart silently.
SCRATCH_ROOT_NAME = ".scratch"


def scratch_independent_key(path: str) -> str:
    """The path with any ``.scratch/<id>/`` segment stripped, POSIX-style.

    ``proj/.scratch/20260805T0522Z/sheets/mipi.kicad_sch``
        -> ``proj/sheets/mipi.kicad_sch``

    Separators are normalised so a Windows and a POSIX checkout of the same
    tree agree on the key.
    """
    parts = list(PurePath(path).as_posix().split("/"))
    if SCRATCH_ROOT_NAME in parts:
        index = parts.index(SCRATCH_ROOT_NAME)
        # Drop ".scratch" and the scratch-id directory right after it.
        del parts[index:index + 2]
    return "/".join(p for p in parts if p not in ("", "."))


def det_uuid(namespace: str, path: str, tag: str) -> str:
    """Stable UUID5 for (tool namespace, scratch-independent path, tag).

    Deterministic so that re-running a tool on an unchanged file produces an
    unchanged diff, and distinct across same-named files under different
    boards. See the module docstring for why neither the bare basename nor
    the full path works.
    """
    return str(_uuid.uuid5(
        _uuid.NAMESPACE_URL,
        f"{namespace}/{scratch_independent_key(path)}/{tag}",
    ))
