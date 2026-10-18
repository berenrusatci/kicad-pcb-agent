"""Shared location constants for the test suite.

Tests used to sit next to the scripts they exercised and found them with
``Path(__file__).with_name(...)``. In the consolidated layout scripts,
schemas and fixtures each live in their own directory, so every test imports
these constants instead of rediscovering the tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TOOLS = TESTS.parent
COMMON = TOOLS / "common"
SCRIPTS = TOOLS / "scripts"
BOARD_SCRIPTS = SCRIPTS / "board"
SCH_SCRIPTS = SCRIPTS / "sch"
SERVERS = TOOLS / "servers"
SCHEMAS = TOOLS / "schemas"
FIXTURES = TOOLS / "fixtures"

REPO = TOOLS.parent            # pcb-agent/
CONFIG = REPO / "config"
TOOL_REGISTRY = CONFIG / "tool_registry.json"

# Make ``common``, the scripts and the servers importable from any test.
for _p in (TOOLS, SCRIPTS, BOARD_SCRIPTS, SCH_SCRIPTS, SERVERS):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
