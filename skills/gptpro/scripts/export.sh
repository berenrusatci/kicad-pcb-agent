#!/usr/bin/env bash
set -euo pipefail

# GPTPro export — thin front end.
#
# This script owns exactly two things:
#   1. the environment/CLI contract (PROJECT_ROOT, OUTPUT_DIR, ...),
#   2. sourcing bundles.conf, which is a bash file that calls define_bundle
#      and may append to EXCLUDES.
#
# Everything downstream — eligibility enumeration, exclusion attribution,
# staging, hashing, coverage arithmetic, content/disclosure scanning, MANIFEST
# and receipt production, atomic publish — lives in gptpro_export.py.
#
# Why the split: the coverage contract requires `scanned + excluded == eligible`
# to hold *exactly*. When rsync stages the files and something else counts them,
# the two can disagree and the arithmetic becomes a claim rather than a
# measurement. One component now both decides and reports, so it cannot drift
# from itself.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$SCRIPT_DIR/gptpro_export.py"

PROJECT_ROOT="${PROJECT_ROOT:-$PWD}"
OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/gptpro-bundles}"
BUNDLES_CONF="${BUNDLES_CONF:-$SCRIPT_DIR/../bundles.conf}"
BUNDLE_PREFIX="${BUNDLE_PREFIX:-$(basename "$PROJECT_ROOT")}"
ALLOW_SECRETS="${ALLOW_SECRETS:-0}"
DISCLOSURE_POLICY="${DISCLOSURE_POLICY:-$PROJECT_ROOT/.gptpro/disclosure-policy.json}"
KEEP_RUNS="${KEEP_RUNS:-5}"
PYTHON="${PYTHON:-python3}"

log()  { printf '[gptpro] %s\n' "$1"; }
warn() { printf '[gptpro] WARN: %s\n' "$1" >&2; }
die()  { printf '[gptpro] ERROR: %s\n' "$1" >&2; exit 1; }

command -v zip     >/dev/null 2>&1 || die "missing required command: zip"
command -v "$PYTHON" >/dev/null 2>&1 || die "missing required command: $PYTHON"
[ -f "$ENGINE" ] || die "missing export engine: $ENGINE"
[ -d "$PROJECT_ROOT" ] || die "PROJECT_ROOT does not exist: $PROJECT_ROOT"

# --- freshness re-check mode -------------------------------------------------
# `export.sh --recheck [run-dir]` does not export anything. It reads a published
# handoff_receipt.json and compares the snapshot it recorded against the live
# tree, so `gptpro-handoff` step 3 can answer "is the uploaded corpus still the
# code I am about to talk about?" with a measurement instead of a memory.
if [ "${1:-}" = "--recheck" ]; then
  exec "$PYTHON" "$ENGINE" --recheck \
    --project-root "$PROJECT_ROOT" \
    --run-dir "${2:-$OUTPUT_DIR/current}"
fi

# Global excludes: vcs noise, dependency trees, caches, credential-file classes,
# binary assets.
#
# `build/dist/out/coverage` stay here on purpose even though in a hardware repo
# `build/` is an evidence root, not compiler noise. The repo config takes them
# back deliberately by naming a build directory as a `dir:` source root — a
# source root's own contents are always staged, so the recovery is explicit and
# visible in the manifest instead of implicit in the tool's defaults.
EXCLUDES=(
  --exclude '.git'
  --exclude 'node_modules'
  --exclude '.next'
  --exclude '.turbo'
  --exclude 'dist'
  --exclude 'build'
  --exclude 'out'
  --exclude 'coverage'
  --exclude '.vercel'
  --exclude '*.tsbuildinfo'
  --exclude '.env'
  --exclude '.env.*'
  --exclude '*.log'
  --exclude '.DS_Store'
  --exclude 'Icon?'
  # Emacs auto-save/backup copies: `#.env#` is a byte-for-byte copy of .env that
  # slips past the `.env` / `.env.*` rules above.
  --exclude '#*#'
  --exclude '.#*'
  --exclude '*~'
  # Binary assets — noise for a code-review model; keep bundles light.
  --exclude '*.png'  --exclude '*.jpg'  --exclude '*.jpeg'
  --exclude '*.gif'  --exclude '*.webp' --exclude '*.avif'
  --exclude '*.ico'  --exclude '*.mp4'  --exclude '*.mov'
  --exclude '*.webm' --exclude '*.woff' --exclude '*.woff2'
  --exclude '*.ttf'  --exclude '*.otf'
)
GLOBAL_EXCLUDE_COUNT="${#EXCLUDES[@]}"

BUNDLE_DECLS=()   # one line per bundle: name<TAB>desc<TAB>src<TAB>src...
MODE="explicit"

# define_bundle <name> "<description>" <source>...
#   sources: dir:<path>[:<extra-exclude>] | file:<path>[:<subdir>]
#   optional: prefix either form with `optional:` to declare it may be absent.
#
# In an explicit config a source is REQUIRED by default: a missing one is a
# coverage loss, not a layout variation, and it fails the export. If a source
# genuinely is variant-dependent, say so with `optional:` — that keeps the
# decision visible in the manifest instead of hiding it behind a warning.
define_bundle() {
  local name="$1"; local desc="$2"; shift 2
  local line="$name"$'\t'"$desc"
  for src in "$@"; do line="$line"$'\t'"$src"; done
  BUNDLE_DECLS+=("$line")
}

# --- autodetect --------------------------------------------------------------
# Zero-config fallback when no bundles.conf exists. Sources are declared
# `optional:` here on purpose: autodetect is guessing at a layout it has never
# seen, so a missing path is an expected miss rather than lost evidence. This is
# the one place where soft behaviour is correct, and it is why it is confined to
# this function.
autodetect_bundles() {
  local found=0 d name

  for parent in apps packages services libs; do
    [ -d "$PROJECT_ROOT/$parent" ] || continue
    for d in "$PROJECT_ROOT/$parent"/*/; do
      [ -d "$d" ] || continue
      name="$(basename "$d")"
      define_bundle "$name" "$parent/$name — workspace package" "optional:dir:$parent/$name"
      found=1
    done
  done

  if [ "$found" -eq 0 ]; then
    define_bundle src "application source" \
      optional:dir:src optional:dir:lib optional:dir:app \
      optional:dir:test optional:dir:tests \
      optional:file:package.json optional:file:tsconfig.json \
      optional:file:pyproject.toml optional:file:go.mod optional:file:Cargo.toml
  fi

  # Schema construction only — never rows or seed data.
  define_bundle db "database architecture: migrations and schema map (no rows)" \
    optional:dir:supabase/migrations optional:dir:migrations \
    optional:dir:db/migrations optional:dir:prisma \
    optional:file:supabase/config.toml:supabase

  define_bundle docs "architecture, ADRs, and conventions" \
    optional:dir:docs optional:dir:adrs optional:dir:doc optional:dir:prds \
    optional:file:README.md optional:file:AGENTS.md optional:file:CLAUDE.md \
    optional:file:pnpm-workspace.yaml optional:file:package.json
}

# --- bundle definitions ------------------------------------------------------
# A hand-written bundles.conf always wins: a split that matches your architecture
# beats anything inferred. Without one, fall back to autodetect so the skill is
# useful in a fresh repo on the first run.
if [ -f "$BUNDLES_CONF" ]; then
  log "bundles: $BUNDLES_CONF"
  # shellcheck disable=SC1090
  source "$BUNDLES_CONF"
else
  log "no bundles.conf — autodetecting layout (write one for a better split)"
  MODE="autodetect"
  BUNDLES_CONF=""
  autodetect_bundles
fi

[ "${#BUNDLE_DECLS[@]}" -gt 0 ] || die "no bundles declared — check $BUNDLES_CONF against your repo layout"

# Hand the whole declaration set to the engine. Excludes appended by bundles.conf
# land after the globals, which is exactly how the engine tells `tool_global`
# from `project_config` when it attributes an exclusion to a policy source.
PLAN_FILE="$(mktemp "${TMPDIR:-/tmp}/gptpro-plan-XXXXXX.txt")"
trap 'rm -f "$PLAN_FILE"' EXIT

{
  printf '%s\n' "$MODE"
  printf '%s\n' "$PROJECT_ROOT"
  printf '%s\n' "$OUTPUT_DIR"
  printf '%s\n' "$BUNDLE_PREFIX"
  printf '%s\n' "$BUNDLES_CONF"
  printf '%s\n' "$DISCLOSURE_POLICY"
  printf '%s\n' "$ALLOW_SECRETS"
  printf '%s\n' "$KEEP_RUNS"
  printf '%s\n' "$GLOBAL_EXCLUDE_COUNT"
  printf '%s\n' "${#EXCLUDES[@]}"
  for e in "${EXCLUDES[@]}"; do printf '%s\n' "$e"; done
  printf '%s\n' "${#BUNDLE_DECLS[@]}"
  for b in "${BUNDLE_DECLS[@]}"; do printf '%s\n' "$b"; done
} > "$PLAN_FILE"

# Deliberately not `exec`: exec replaces this shell, and the EXIT trap that
# removes the plan file would never fire.
"$PYTHON" "$ENGINE" --plan "$PLAN_FILE"
exit $?
