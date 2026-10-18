#!/usr/bin/env bash
# One command that runs EVERYTHING. No pytest required.
#
# The old bundle had no such command: five test files in three directories,
# each invoked by hand, two of them findable only by cd-ing next to them.
#
#   ./run_tests.sh              run every test
#   ./run_tests.sh -v           verbose
#   ./run_tests.sh test_common  run one module
#
# Exit codes: 0 all passed, 1 a test failed, 2 the runner could not run.
set -uo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$TOOLS_DIR")"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON not found" >&2
    exit 2
fi

# Tests import `common` and the scripts directly; the runner must be able to
# find them whichever directory the caller started from.
export PYTHONPATH="$TOOLS_DIR:$TOOLS_DIR/scripts:$TOOLS_DIR/scripts/board:$TOOLS_DIR/scripts/sch:$TOOLS_DIR/servers:${PYTHONPATH:-}"

# Several tests shell out to the tools with cwd=repo root.
cd "$REPO_DIR" || exit 2

echo "== environment =="
"$PYTHON" - <<'PY'
import importlib.util, shutil, sys
print(f"python      {sys.version.split()[0]}")
print(f"kicad-cli   {shutil.which('kicad-cli') or 'NOT FOUND (some tests skip)'}")
have = importlib.util.find_spec("pcbnew") is not None
print(f"pcbnew      {'available' if have else 'NOT FOUND (board tests skip)'}")
print(f"pytest      {'available (optional)' if importlib.util.find_spec('pytest') else 'not installed (not required)'}")
PY
echo

if [ "$#" -gt 0 ] && [[ "$1" != -* ]]; then
    exec "$PYTHON" -m unittest "tests.$1" "${@:2}"
fi

echo "== unittest discover =="
"$PYTHON" -m unittest discover -s "$TOOLS_DIR/tests" -t "$TOOLS_DIR" -p 'test_*.py' "$@"
status=$?

echo
if [ "$status" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "TESTS FAILED (exit $status)"
fi
exit "$status"
