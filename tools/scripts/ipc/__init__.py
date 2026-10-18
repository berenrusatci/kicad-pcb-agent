"""IPC standards calculators and rule engines.

Ported from the pcb-tool-v2 archive. Every module here is stdlib-only and
runnable both as ``python3 tools/scripts/ipc/<name>.py`` and as
``from scripts.ipc import <name>``.

None of these tools is a compliance proof. Each one carries its own
limitation notes in its docstring and repeats the material ones into its
JSON report under ``limits`` so that a downstream consumer cannot lose them.
"""
