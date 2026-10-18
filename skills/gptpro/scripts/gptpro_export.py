#!/usr/bin/env python3
"""GPTPro export engine — staging, coverage accounting, manifests, receipt.

Invoked by ``export.sh``; not meant to be called by hand (it takes a plan file
the shell front end writes). ``export.sh --recheck`` routes here too.

WHAT THIS TOOL IS AUTHORIZED TO ASSERT
--------------------------------------
Exactly one sentence: *this snapshot carries this declared scope at these
hashes.* It does not judge a PCB, an ERC rule, or a finding. Its GREEN is a
handoff-integrity verdict and nothing else.

WHY ONE COMPONENT DOES BOTH THE STAGING AND THE COUNTING
--------------------------------------------------------
The coverage contract (``pcb-agent/skills/_shared/coverage-gate.md``) requires

    scanned + excluded == eligible

to hold exactly, per bundle, in files *and* in bytes. If rsync copies and
something else counts, the identity is an assertion about another program's
behaviour. Here the same pass decides inclusion and records it, so the
arithmetic is a measurement of what actually happened.

Stdlib only, like every other tool in this repo.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = "gptpro-handoff/v1"
GENERATOR = "skills/gptpro/scripts/gptpro_export.py"

# Verdicts are drawn from the CLOSED vocabulary in
# pcb-agent/skills/_shared/coverage-gate.md. Nothing new is minted here.
V_GREEN = "GREEN"
V_RED = "RED"
V_CONFIRM = "CONFIRM"
V_NO_COVERAGE = "NO_COVERAGE"
V_INVALID_COVERAGE = "INVALID_COVERAGE"
V_STALE = "STALE"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[gptpro] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[gptpro] WARN: {msg}", file=sys.stderr, flush=True)


class Fatal(Exception):
    """Aborts the export. Carries the verdict the receipt would have had."""

    def __init__(self, verdict: str, message: str):
        super().__init__(message)
        self.verdict = verdict


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# exclusion matching
# --------------------------------------------------------------------------
#
# A deliberately small, documented subset of rsync's filter language — the four
# forms the config files actually use:
#
#   PAT       no slash   -> matches any single path component (dir or file name)
#   a/b       slash      -> matches any contiguous run of components
#   /a/b      anchored   -> matches from the source root only
#   PAT/      trailing / -> same as PAT (we only ever exclude whole subtrees)
#
# Patterns are matched against the path RELATIVE TO THE SOURCE ROOT. That is
# what makes `dir:cam-a-og05b/build` legal while `build` is a global exclude:
# the root's own name is not part of the matched path, so naming a build
# directory as a source root is an explicit, manifest-visible recovery of
# evidence the global default would otherwise drop.

def pattern_matches(pattern: str, relpath: str) -> bool:
    pat = pattern.rstrip("/")
    if not pat:
        return False
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    if not pat:
        return False

    parts = relpath.split("/")

    if "/" not in pat:
        if anchored:
            return fnmatch.fnmatchcase(parts[0], pat)
        return any(fnmatch.fnmatchcase(p, pat) for p in parts)

    if anchored:
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        return any(fnmatch.fnmatchcase(c, pat) for c in prefixes)

    runs = (
        "/".join(parts[i:j])
        for i in range(len(parts))
        for j in range(i + 1, len(parts) + 1)
    )
    return any(fnmatch.fnmatchcase(c, pat) for c in runs)


# Why a pattern exists, for the exclusion inventory. A model that can see
# "dropped 141 files matching *.pdf because binary vendor material is not
# cleared for third-party upload" knows something a bare count never tells it:
# the difference between absent and withheld.
REASON_TABLE: list[tuple[str, str]] = [
    (r"^\.git$", "version-control internals"),
    (r"^node_modules$|^\.next$|^\.turbo$|^\.vercel$", "dependency/framework build tree"),
    (r"^dist$|^build$|^out$|^coverage$",
     "generic build-output directory name; a build root that carries evidence is "
     "taken back by naming it as an explicit dir: source"),
    (r"^\.env", "credential file class"),
    (r"^#\*#$|^\.#\*$|^\*~$", "editor auto-save copy of a possibly-credential file"),
    (r"^\*\.log$", "log noise"),
    (r"^\.DS_Store$|^Icon\?$", "OS metadata"),
    (r"^\*\.tsbuildinfo$", "compiler cache"),
    (r"^\*\.(png|jpg|jpeg|gif|webp|avif|ico|mp4|mov|webm|woff2?|ttf|otf)$",
     "binary asset; unreadable to a text model"),
    (r"^\*\.(pdf|xlsx|xls|docx?|zip|step|stp|dxf)$",
     "binary vendor/producer material; NOT cleared for third-party upload"),
    (r"^\*\.(bak|kicad_prl)$|^\*\.lck$|^fp-info-cache$|^\*-backups$|^\.history$",
     "KiCad editor/backup noise"),
    (r"^__pycache__$|^\.pytest_cache$|^\.ruff_cache$", "python tool cache"),
    (r"^\*\.svg$|^svg$", "rendered vector output"),
    (r"^fault$", "fault subtree excluded at this source only; the high-signal "
                 "summary is taken back explicitly"),
]


SOURCE_DECL_REASON = (
    "excluded at this source only, by the bundle declaration — this scope is "
    "normally carried by a sibling bundle; check sibling_bundles[] before "
    "concluding the repo does not contain it"
)


def reason_for(pattern: str, policy_source: str = "tool_global") -> str:
    if policy_source == "source_decl":
        return SOURCE_DECL_REASON
    for rx, reason in REASON_TABLE:
        if re.match(rx, pattern):
            return reason
    return "pattern-based exclusion (no reason recorded in the tool's reason table)"


# --------------------------------------------------------------------------
# content policy — applied AFTER staging, to every staged file
# --------------------------------------------------------------------------
#
# The old design scanned only what `dir:` sources produced, because exclusions
# were an rsync flag and `file:` sources used a plain cp. That made a file's
# treatment depend on how it was declared rather than on what it contains: the
# same .pdf was forbidden through `dir:` and free through `file:`. Inclusion
# mechanism is not a policy. These classes are checked on the staged tree, so
# every path into the bundle goes through the same gate.

RESTRICTED_SUFFIXES = {
    ".pdf": "binary vendor/producer document",
    ".xlsx": "binary vendor/producer spreadsheet",
    ".xls": "binary vendor/producer spreadsheet",
    ".doc": "binary vendor/producer document",
    ".docx": "binary vendor/producer document",
    ".step": "3D mechanical model (vendor-derived geometry)",
    ".stp": "3D mechanical model (vendor-derived geometry)",
    ".dxf": "mechanical drawing exchange file",
    ".p12": "credential container",
    ".pfx": "credential container",
    ".pem": "credential container",
    ".key": "credential container",
}
RESTRICTED_NAMES = {
    ".env": "credential file",
    "id_rsa": "private key",
    "id_ed25519": "private key",
    ".netrc": "credential file",
}

HARD_CREDENTIAL = re.compile(
    r"BEGIN [A-Z ]*PRIVATE KEY"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
    r"|sk-ant-[A-Za-z0-9_-]{40,}|sk-[A-Za-z0-9]{40,}"
    r"|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}"
    r"|xox[baprs]-[A-Za-z0-9-]{20,}|AIza[0-9A-Za-z_-]{30,}"
    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\."
)
SOFT_CREDENTIAL = re.compile(
    r"(password|passwd|secret|api_?key|access_?token)[ \t]*[=:][ \t]*[\"'][^\"'\s]{8,}",
    re.IGNORECASE,
)
FIXTURE_MARKER = re.compile(
    r"FAKE|EXAMPLE|PLACEHOLDER|DUMMY|REDACTED|SAMPLE|NOT_?A_?REAL"
    r"|xxxxxxxx|XXXXXXXX|0123456789|abcdefghij|env\("
)
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|fixtures|spec)/|\.(test|spec)\.[a-z]+$")


def classify_kind(repo_path: str) -> str:
    """Coarse routing hint for the reader, not an authority claim.

    A model asking "is this a measurement or a policy statement" gets a first
    answer without opening the file. It is a hint: `kind` never decides a gate.
    """
    p = repo_path
    low = p.lower()
    if "/artifacts/" in p or p.startswith("artifacts/") or "/build/" in p or p.startswith("build/"):
        return "evidence"
    if re.search(r"(gate_receipt|_receipt|erc|drc|netlist|no_connect|collision|coverage|fault)"
                 r"[^/]*\.(json|net|txt|xml)$", low):
        return "evidence"
    if low.endswith("skill.md") or "/skills/" in low or low.endswith(".conf") \
            or low.endswith(".schema.json") or "/config/" in low:
        return "policy"
    if low.endswith("manifest.json") or "_index.json" in low:
        return "derived"
    return "raw"


# --------------------------------------------------------------------------
# plan parsing
# --------------------------------------------------------------------------

class Source:
    __slots__ = ("kind", "path", "extra", "required", "decl")

    def __init__(self, decl: str):
        self.decl = decl
        rest = decl
        self.required = True
        if rest.startswith("optional:"):
            self.required = False
            rest = rest[len("optional:"):]
        if ":" not in rest:
            raise Fatal(V_RED, f"malformed source declaration (expected dir:/file:): {decl!r}")
        kind, rest = rest.split(":", 1)
        if kind not in ("dir", "file"):
            raise Fatal(V_RED, f"unknown source type {kind!r} in declaration {decl!r}")
        self.kind = kind
        if ":" in rest:
            self.path, self.extra = rest.split(":", 1)
        else:
            self.path, self.extra = rest, ""
        if not self.path:
            raise Fatal(V_RED, f"empty path in source declaration {decl!r}")


class Bundle:
    def __init__(self, name: str, desc: str, sources: list[Source]):
        self.name = name
        self.desc = desc
        self.sources = sources


class Plan:
    def __init__(self, path: Path):
        lines = path.read_text(encoding="utf-8").split("\n")
        it = iter(lines)

        def nxt() -> str:
            return next(it)

        self.mode = nxt()
        self.project_root = Path(nxt()).resolve()
        self.output_dir = Path(nxt())
        self.bundle_prefix = nxt()
        conf = nxt()
        self.bundles_conf = Path(conf).resolve() if conf else None
        pol = nxt()
        self.disclosure_policy = Path(pol) if pol else None
        self.allow_secrets = nxt().strip() == "1"
        self.keep_runs = max(1, int(nxt().strip() or "5"))

        global_count = int(nxt().strip())
        total = int(nxt().strip())
        raw = [nxt() for _ in range(total)]

        # EXCLUDES is an rsync-style flat array: --exclude PATTERN --exclude ...
        # Its index is what tells a tool default from a project decision, which
        # is exactly the `policy_source` the exclusion inventory has to carry.
        self.excludes: list[tuple[str, str]] = []  # (pattern, policy_source)
        i = 0
        while i < len(raw):
            if raw[i] == "--exclude" and i + 1 < len(raw):
                src = "tool_global" if i < global_count else "project_config"
                self.excludes.append((raw[i + 1], src))
                i += 2
            else:
                warn(f"ignoring unrecognised EXCLUDES entry: {raw[i]!r}")
                i += 1

        nbundles = int(nxt().strip())
        self.bundles: list[Bundle] = []
        for _ in range(nbundles):
            fields = nxt().split("\t")
            name, desc, decls = fields[0], fields[1], fields[2:]
            if not decls:
                raise Fatal(V_NO_COVERAGE, f"bundle {name!r} declares no sources")
            self.bundles.append(Bundle(name, desc, [Source(d) for d in decls]))

        names = [b.name for b in self.bundles]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise Fatal(V_RED, f"duplicate bundle names in config: {sorted(dupes)}")


# --------------------------------------------------------------------------
# snapshot identity
# --------------------------------------------------------------------------

def git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def snapshot_identity(root: Path) -> dict:
    """repo_commit + an honest description of how far the tree has drifted.

    A commit SHA alone describes a dirty tree the way a receipt describes a
    shoplifted item: accurately, and about the wrong object. When the tree is
    dirty the export records a hash over the full porcelain status so a
    consumer can at least tell *whether* it is looking at the same drift.
    """
    head = git(root, "rev-parse", "HEAD")
    if head is None:
        return {
            "vcs": None,
            "repo_commit": None,
            "branch": None,
            "dirty": None,
            "dirty_inventory_sha256": None,
            "tracked_modified": None,
            "untracked": None,
            "note": "PROJECT_ROOT is not a git worktree — no snapshot identity available",
        }
    branch = (git(root, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    status = git(root, "status", "--porcelain=v1", "-uall") or ""
    entries = sorted(line for line in status.split("\n") if line.strip())
    tracked_modified = sum(1 for e in entries if not e.startswith("??"))
    untracked = sum(1 for e in entries if e.startswith("??"))
    return {
        "vcs": "git",
        "repo_commit": head.strip(),
        "branch": branch,
        "dirty": bool(entries),
        "dirty_inventory_sha256": sha256_text("\n".join(entries)) if entries else None,
        "tracked_modified": tracked_modified,
        "untracked": untracked,
        "note": None,
    }


# --------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------

class StagedFile:
    __slots__ = ("bundle_path", "repo_path", "size", "source_decl")

    def __init__(self, bundle_path: str, repo_path: str, size: int, source_decl: str):
        self.bundle_path = bundle_path
        self.repo_path = repo_path
        self.size = size
        self.source_decl = source_decl


def enumerate_dir(root: Path) -> tuple[list[tuple[str, int]], list[str]]:
    """Every regular file under `root`, relative to it. Symlinks are reported
    separately: they are neither staged nor counted as eligible, and a silent
    drop is the class of omission this whole rewrite exists to remove."""
    files: list[tuple[str, int]] = []
    links: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in sorted(dirnames):
            full = Path(dirpath) / d
            if full.is_symlink():
                links.append(f"{rel_dir}/{d}" if rel_dir else d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = Path(dirpath) / f
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if full.is_symlink():
                links.append(rel)
                continue
            try:
                files.append((rel, full.stat().st_size))
            except OSError as exc:
                raise Fatal(V_RED, f"cannot stat eligible file {full}: {exc}") from exc
    return files, links


def stage_bundle(plan: Plan, bundle: Bundle, staging_root: Path) -> dict:
    """Stage one bundle and account for every eligible byte on the way through."""
    dest_root = staging_root / bundle.name
    dest_root.mkdir(parents=True, exist_ok=True)

    staged: list[StagedFile] = []
    excluded: list[dict] = []          # per-file exclusion records
    symlinks: list[str] = []
    sources_report: list[dict] = []
    missing_required: list[str] = []
    eligible_files = 0
    eligible_bytes = 0
    seen_dest: dict[str, str] = {}

    for src in bundle.sources:
        abs_src = plan.project_root / src.path
        disposition = "required" if src.required else "optional"
        exists = abs_src.is_dir() if src.kind == "dir" else abs_src.is_file()

        if not exists:
            sources_report.append({
                "decl": src.decl, "kind": src.kind, "path": src.path,
                "disposition": disposition, "present": False,
                "eligible_files": 0, "eligible_bytes": 0,
                "scanned_files": 0, "scanned_bytes": 0,
            })
            if src.required:
                missing_required.append(src.decl)
            else:
                log(f"  {bundle.name}: optional source absent — {src.path}")
            continue

        # Exclusion rules that apply to this source: the tool globals, the
        # project's additions, and the source's own extra pattern last.
        rules = list(plan.excludes)
        if src.kind == "dir" and src.extra:
            rules.append((src.extra, "source_decl"))

        if src.kind == "dir":
            entries, links = enumerate_dir(abs_src)
            symlinks.extend(f"{src.path}/{link}" for link in links)
        else:
            entries = [(abs_src.name, abs_src.stat().st_size)]
            links = []

        s_files = s_bytes = 0
        for rel, size in entries:
            eligible_files += 1
            eligible_bytes += size

            hit = None
            if src.kind == "dir":
                for pattern, policy_source in rules:
                    if pattern_matches(pattern, rel):
                        hit = (pattern, policy_source)
                        break
            # `file:` sources are not exclusion-filtered: naming a single file
            # IS the inclusion decision. The safety that used to be missing is
            # not restored here but in the post-staging content policy, which
            # every file goes through regardless of how it got in.

            repo_path = f"{src.path}/{rel}" if src.kind == "dir" else src.path
            if hit:
                excluded.append({
                    "repo_path": repo_path,
                    "pattern": hit[0],
                    "policy_source": hit[1],
                    "reason": reason_for(hit[0], hit[1]),
                    "bytes": size,
                    "source_decl": src.decl,
                })
                continue

            if src.kind == "dir":
                bundle_rel = f"{src.path}/{rel}"
            else:
                bundle_rel = f"{src.extra}/{abs_src.name}" if src.extra else abs_src.name

            if bundle_rel in seen_dest:
                raise Fatal(
                    V_RED,
                    f"bundle {bundle.name!r} stages two sources onto the same path "
                    f"{bundle_rel!r} ({seen_dest[bundle_rel]!r} and {src.decl!r}); "
                    f"one would silently overwrite the other"
                )
            seen_dest[bundle_rel] = src.decl

            target = dest_root / bundle_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = abs_src / rel if src.kind == "dir" else abs_src
            shutil.copy2(source_file, target)
            staged.append(StagedFile(bundle_rel, repo_path, size, src.decl))
            s_files += 1
            s_bytes += size

        sources_report.append({
            "decl": src.decl, "kind": src.kind, "path": src.path,
            "disposition": disposition, "present": True,
            "eligible_files": len(entries),
            "eligible_bytes": sum(sz for _, sz in entries),
            "scanned_files": s_files, "scanned_bytes": s_bytes,
            "symlinks_skipped": len(links),
        })

    return {
        "bundle": bundle,
        "staged": staged,
        "excluded": excluded,
        "symlinks": symlinks,
        "sources": sources_report,
        "missing_required": missing_required,
        "eligible_files": eligible_files,
        "eligible_bytes": eligible_bytes,
    }


# --------------------------------------------------------------------------
# content policy + credential scan (post-staging, every file)
# --------------------------------------------------------------------------

def scan_staged(results: list[dict], staging_root: Path, allow_secrets: bool) -> dict:
    """Two separate scopes, reported separately — because they answer different
    questions and the old tool merged them.

    `credential_scan` answers "does a live-looking credential appear in the
    staged bytes". That is all it has ever answered. Saying "clean" about it and
    letting the reader hear "safe to upload" is the failure this split removes.

    `content_policy` answers "did a file class that must never leave the repo
    reach the staging tree, no matter which source kind carried it".
    """
    hard_hits: list[dict] = []
    soft_hits = 0
    scanned = 0
    unreadable = 0
    violations: list[dict] = []

    for res in results:
        bundle = res["bundle"]
        for sf in res["staged"]:
            path = staging_root / bundle.name / sf.bundle_path
            suffix = Path(sf.bundle_path).suffix.lower()
            name = Path(sf.bundle_path).name

            if suffix in RESTRICTED_SUFFIXES:
                violations.append({
                    "bundle": bundle.name, "repo_path": sf.repo_path,
                    "class": RESTRICTED_SUFFIXES[suffix],
                    "source_decl": sf.source_decl,
                })
            elif name in RESTRICTED_NAMES:
                violations.append({
                    "bundle": bundle.name, "repo_path": sf.repo_path,
                    "class": RESTRICTED_NAMES[name],
                    "source_decl": sf.source_decl,
                })

            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                unreadable += 1
                continue

            for lineno, line in enumerate(text.split("\n"), 1):
                if HARD_CREDENTIAL.search(line):
                    demoted = bool(FIXTURE_MARKER.search(line)) or bool(TEST_PATH.search(sf.repo_path))
                    if demoted:
                        soft_hits += 1
                    else:
                        hard_hits.append({
                            "bundle": bundle.name, "repo_path": sf.repo_path,
                            "line": lineno,
                        })
                elif SOFT_CREDENTIAL.search(line):
                    soft_hits += 1

    credential = {
        "scope": "credential_scan",
        "asserts": "no live-looking provider credential shape appears in the staged bytes",
        "does_not_assert": "that the staged corpus is cleared for third-party disclosure "
                           "— that is the disclosure_gate scope",
        "coverage_files": {
            "eligible": scanned, "scanned": scanned - unreadable,
            "excluded": unreadable, "violations": len(hard_hits),
        },
        "hard_hits": hard_hits,
        "soft_hit_count": soft_hits,
        "allow_secrets_override": bool(allow_secrets and hard_hits),
    }
    content = {
        "scope": "content_policy",
        "asserts": "no restricted file class reached the staging tree, whatever "
                   "source kind declared it",
        "coverage_files": {
            "eligible": scanned, "scanned": scanned,
            "excluded": 0, "violations": len(violations),
        },
        "violations": violations,
    }
    return {"credential_scan": credential, "content_policy": content}


# --------------------------------------------------------------------------
# disclosure gate
# --------------------------------------------------------------------------

def disclosure_gate(policy_path: Path | None, results: list[dict]) -> dict:
    """Is this corpus cleared to leave the repo for a third-party chat UI?

    OPEN QUESTION K-1 in the redesign spec: the canonical source of that
    authority — licence terms, an NDA release, an operator allowlist — is not
    defined for this repo. A filesystem regex cannot supply it. So with no
    policy record on disk the honest answer is UNKNOWN, and UNKNOWN is CONFIRM
    with upload unauthorized. It is deliberately NOT GREEN, and there is no
    environment variable that turns it into GREEN: an override switch here would
    reintroduce exactly the laundering this gate exists to stop.

    To close it, an authority writes .gptpro/disclosure-policy.json:

        {"authority": "<who decided, and on what basis>",
         "decided_utc": "2026-08-10T00:00:00Z",
         "rules": [{"pattern": "pcb-agent", "share": "allow",
                    "class": "PROJECT_OWNED", "basis": "..."}]}

    First matching rule wins. `share` has THREE values, not two:

        allow    cleared to leave the repo
        deny     must not leave -> RED, the export aborts
        confirm  classified, and the answer is "authority undefined" -> CONFIRM

    `confirm` is the one that earns its keep. Without it, a class nobody has
    the standing to clear has only two homes: `deny`, which stops the export
    over a question no one asked, or silence, which is indistinguishable from
    never having looked. `confirm` says the third thing — *somebody examined
    this and the authority is genuinely missing* — and that is a different
    fact from an unmatched path, which stays UNKNOWN.

    Both land on CONFIRM. They are reported separately because "we know we
    don't know" and "we didn't look" should never be the same number.
    """
    staged_paths = [
        (res["bundle"].name, sf.repo_path)
        for res in results for sf in res["staged"]
    ]
    total = len(staged_paths)

    if policy_path is None or not policy_path.is_file():
        return {
            "status": V_CONFIRM,
            "upload_authorized": False,
            "authority": None,
            "policy_path": str(policy_path) if policy_path else None,
            "policy_sha256": None,
            "coverage_files": {
                "eligible": total, "scanned": 0, "excluded": total, "violations": 0,
            },
            "allowed": 0,
            "confirm_required": 0,
            "confirm_classes": [],
            "unclassified": total,
            "denied": [],
            "reason": "no disclosure policy record on disk; the canonical third-party "
                      "sharing authority for this repo is undefined (spec section K, "
                      "open question 1). UNKNOWN is CONFIRM, never GREEN.",
        }

    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Fatal(V_RED, f"disclosure policy is present but unreadable: {exc}") from exc

    authority = policy.get("authority")
    rules = policy.get("rules") or []
    if not authority or not isinstance(rules, list) or not rules:
        raise Fatal(
            V_RED,
            "disclosure policy must carry a non-empty `authority` string and a "
            "non-empty `rules` array; an unattributed policy is not an authority",
        )

    for i, rule in enumerate(rules):
        share = rule.get("share")
        if share not in ("allow", "deny", "confirm"):
            # Guessing here is how a typo becomes a disclosure. An unreadable
            # rule is a broken policy, not a permissive one.
            raise Fatal(
                V_RED,
                f"disclosure policy rule {i} has share={share!r}; expected one of "
                f"allow / deny / confirm",
            )

    denied: list[dict] = []
    confirm_required: list[dict] = []
    unclassified: list[str] = []
    allowed = 0
    for bundle_name, repo_path in staged_paths:
        decision = None
        for rule in rules:
            pattern = rule.get("pattern", "")
            if pattern and (fnmatch.fnmatchcase(repo_path, pattern)
                            or pattern_matches(pattern, repo_path)):
                decision = rule
                break
        if decision is None:
            unclassified.append(repo_path)
            continue
        record = {
            "bundle": bundle_name, "repo_path": repo_path,
            "class": decision.get("class"), "basis": decision.get("basis"),
        }
        share = decision["share"]
        if share == "allow":
            allowed += 1
        elif share == "confirm":
            confirm_required.append(record)
        else:
            denied.append(record)

    classified = allowed + len(confirm_required) + len(denied)

    if denied:
        status, authorized, reason = V_RED, False, "policy denies disclosure of staged files"
    elif confirm_required or unclassified:
        status, authorized = V_CONFIRM, False
        parts = []
        if confirm_required:
            classes = sorted({r["class"] for r in confirm_required if r["class"]})
            parts.append(
                f"{len(confirm_required)} staged files are classified "
                f"{'/'.join(classes) or 'confirm-required'} — the policy records that "
                f"no authority has cleared this class, so it stays CONFIRM"
            )
        if unclassified:
            parts.append(
                f"{len(unclassified)} staged files match no policy rule; unmatched is "
                f"UNKNOWN, which is also CONFIRM but a different fact"
            )
        reason = "; ".join(parts)
    else:
        status, authorized, reason = V_GREEN, True, "every staged file classified as shareable"

    return {
        "status": status,
        "upload_authorized": authorized,
        "authority": authority,
        "policy_path": str(policy_path),
        "policy_sha256": sha256_file(policy_path),
        "decided_utc": policy.get("decided_utc"),
        "coverage_files": {
            "eligible": total, "scanned": classified,
            "excluded": len(unclassified), "violations": len(denied),
        },
        "allowed": allowed,
        "confirm_required": len(confirm_required),
        "confirm_classes": sorted({r["class"] for r in confirm_required if r["class"]}),
        "confirm_sample": sorted(r["repo_path"] for r in confirm_required)[:25],
        "unclassified": len(unclassified),
        "unclassified_sample": sorted(unclassified)[:25],
        "denied": denied,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# derived views
# --------------------------------------------------------------------------
#
# A derived view is a new surface to be wrong on. The 20.9 MB corpus is
# expensive to search, so an index earns its place — but only under three rules,
# because "clean summary, unchecked backing" is how a retrieval aid becomes a
# laundering step:
#
#   1. Every row names its backing file AND that file's sha256.
#   2. Every field is a LITERAL COPY of a top-level key in the source JSON.
#      Nothing is inferred, summarised, or renamed. If a receipt has no
#      `verdict` key, the row has no verdict — it does not get one derived from
#      surrounding fields.
#   3. authority = screen_only. It speeds up finding the evidence; it is never
#      the evidence.
#
# Only EVIDENCE_INDEX is generated. DESIGN_INDEX (sheet graph, refdes, nets,
# diff-pair/netclass binding) is deliberately NOT: it needs real KiCad
# S-expression interpretation, and a design index that is subtly wrong about a
# net is worse than no design index at all. The raw .kicad_sch/.kicad_pcb files
# ship in full and remain the backing store.

INDEX_FIELDS = (
    "schema_version", "verdict", "status", "gate", "result", "pass",
    "coverage", "coverage_files", "metrics", "violations", "blockers",
    "generated_at", "board", "rule", "check",
)

# A value bigger than this is the measurement itself, not a pointer to it — a
# full `violations` array copied into the index would make the index a second
# copy of the corpus, which is the opposite of the job. Oversized values are
# replaced by an explicit, mechanical description of what was left out. Not a
# truncation: nothing is silently shortened, and the row still says how big the
# real value is and where to read it.
INDEX_VALUE_BUDGET_BYTES = 400


def index_value(value):
    blob = json.dumps(value, ensure_ascii=False)
    if len(blob) <= INDEX_VALUE_BUDGET_BYTES:
        return value
    described = {
        "__not_copied": "value exceeds the index budget; read the backing file",
        "json_type": type(value).__name__,
        "serialized_bytes": len(blob),
    }
    if isinstance(value, (list, dict)):
        described["length"] = len(value)
    return described


def build_evidence_index(res: dict, staging_root: Path, generator_sha: str) -> dict | None:
    bundle = res["bundle"]
    rows = []
    for sf in sorted(res["staged"], key=lambda s: s.bundle_path):
        if not sf.bundle_path.endswith(".json"):
            continue
        if classify_kind(sf.repo_path) != "evidence":
            continue
        path = staging_root / bundle.name / sf.bundle_path
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        digest = sha256_file(path)
        fields = {k: index_value(doc[k]) for k in INDEX_FIELDS if k in doc}
        rows.append({
            "bundle_path": sf.bundle_path,
            "repo_path": sf.repo_path,
            "derived_from": [f"{sf.repo_path}@sha256:{digest}"],
            "top_level_keys": sorted(doc.keys()),
            "fields": fields,
            "authority": "screen_only",
        })
    if not rows:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "bundle_name": bundle.name,
        "authority": "screen_only",
        "generator": GENERATOR,
        "generator_sha256": generator_sha,
        "contract": (
            "Every `fields` value is a verbatim copy of a top-level key in the "
            "backing JSON named by derived_from. Nothing here is inferred, "
            "summarised or renamed, and a file with no `verdict` key gets no "
            "verdict. A value too large to index is replaced by an explicit "
            "`__not_copied` marker carrying its type and size — never silently "
            "shortened. Use this to FIND the evidence; re-read the backing file "
            "before relying on it. authority=screen_only — this index cannot "
            "close a gate."
        ),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# manifests, guide, receipt
# --------------------------------------------------------------------------

def exclusion_inventory(records: list[dict]) -> list[dict]:
    """Roll per-file exclusions up by (pattern, policy_source).

    Per-file paths stay out of the model-facing summary on purpose: the point of
    a withheld file is that its existence, not its name, is what the reader is
    owed. The operator's receipt keeps the full list.
    """
    agg: dict[tuple[str, str], dict] = {}
    for rec in records:
        key = (rec["pattern"], rec["policy_source"])
        slot = agg.setdefault(key, {
            "pattern": rec["pattern"],
            "policy_source": rec["policy_source"],
            "reason": rec["reason"],
            "count": 0,
            "bytes": 0,
            "top_level_scopes": set(),
        })
        slot["count"] += 1
        slot["bytes"] += rec["bytes"]
        slot["top_level_scopes"].add(rec["repo_path"].split("/")[0])
    out = []
    for slot in agg.values():
        slot["top_level_scopes"] = sorted(slot["top_level_scopes"])
        out.append(slot)
    return sorted(out, key=lambda s: (-s["bytes"], s["pattern"]))


def build_manifest(res: dict, ctx: dict, all_bundles: list[dict]) -> dict:
    bundle = res["bundle"]
    files = []
    for sf in sorted(res["staged"], key=lambda s: s.bundle_path):
        path = ctx["staging_root"] / bundle.name / sf.bundle_path
        files.append({
            "path": sf.bundle_path,
            "repo_path": sf.repo_path,
            "size_bytes": sf.size,
            "sha256": sha256_file(path),
            "source_decl": sf.source_decl,
            "kind": classify_kind(sf.repo_path),
        })

    scanned_files = len(res["staged"])
    scanned_bytes = sum(sf.size for sf in res["staged"])
    excluded_files = len(res["excluded"])
    excluded_bytes = sum(r["bytes"] for r in res["excluded"])
    violations = [
        v for v in ctx["scan"]["content_policy"]["violations"] if v["bundle"] == bundle.name
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": ctx["snapshot_id"],
        "bundle_name": bundle.name,
        "bundle_purpose": bundle.desc,
        "authority": {
            "asserts": "this bundle carries the declared scope below at these hashes",
            "does_not_assert": [
                "that the PCB design is correct",
                "that a finding about it is true",
                "that the corpus is cleared for third-party disclosure "
                "(see handoff_receipt.json disclosure_gate)",
            ],
        },
        "repo_commit": ctx["identity"]["repo_commit"],
        "branch": ctx["identity"]["branch"],
        "dirty": ctx["identity"]["dirty"],
        "dirty_inventory_sha256": ctx["identity"]["dirty_inventory_sha256"],
        "export_started_utc": ctx["started_utc"],
        "export_finished_utc": ctx["finished_utc"],
        "bundles_conf_sha256": ctx["bundles_conf_sha256"],
        "export_tool_sha256": ctx["export_tool_sha256"],
        "export_tool_files": ctx["export_tool_files"],
        "generator": GENERATOR,
        "sources": res["sources"],
        "files": files,
        "coverage_files": {
            "eligible": res["eligible_files"],
            "scanned": scanned_files,
            "excluded": excluded_files,
            "violations": len(violations),
        },
        "coverage_bytes": {
            "eligible": res["eligible_bytes"],
            "scanned": scanned_bytes,
            "excluded": excluded_bytes,
            "violations": sum(f["size_bytes"] for f in files
                              if any(v["repo_path"] == f["repo_path"] for v in violations)),
        },
        "exclusions": exclusion_inventory(res["excluded"]),
        "symlinks_skipped": sorted(res["symlinks"]),
        "missing_sources": res["missing_required"],
        "optional_sources_absent": [
            s["decl"] for s in res["sources"]
            if not s["present"] and s["disposition"] == "optional"
        ],
        "derived_views": ctx["derived_views"].get(bundle.name, []),
        "sibling_bundles": [
            {"name": b["bundle"].name, "purpose": b["bundle"].desc}
            for b in all_bundles if b["bundle"].name != bundle.name
        ],
        "blind_spot_note": (
            "MANIFEST.json is not listed in files[] — a file cannot carry its own "
            "hash. Its hash is in handoff_receipt.json, which is published outside "
            "the zips. If a path you expect is absent, check exclusions[] and "
            "sibling_bundles[] before concluding the repo does not contain it."
        ),
    }


def check_coverage(manifest: dict, bundle_name: str) -> list[str]:
    """The arithmetic from _shared/coverage-gate.md, enforced not just published."""
    problems = []
    for axis in ("coverage_files", "coverage_bytes"):
        c = manifest[axis]
        if c["scanned"] + c["excluded"] != c["eligible"]:
            problems.append(
                f"{bundle_name}.{axis}: scanned({c['scanned']}) + excluded({c['excluded']}) "
                f"!= eligible({c['eligible']})"
            )
        if c["scanned"] > c["eligible"]:
            problems.append(f"{bundle_name}.{axis}: scanned > eligible")
        if c["violations"] > c["scanned"]:
            problems.append(f"{bundle_name}.{axis}: violations > scanned")
    return problems


def render_guide(receipt: dict, manifests: dict[str, dict]) -> str:
    """00-OKUNACAK.md, generated.

    The hand-written predecessor described seven zips next to a set of eight.
    A guide that is maintained by hand is a claim about the product; this one is
    a projection of it, and the test suite asserts its bundle list equals the
    receipt's.
    """
    ident = receipt["snapshot"]
    lines: list[str] = []
    lines.append(f"# {receipt['bundle_prefix']} — GPT Pro yükleme paketi")
    lines.append("")
    lines.append(
        "> **Bu dosya elle düzenlenmez.** `gptpro` export'u her koşuda manifestlerden "
        "üretir. Elle tutulan bir öncekisi 8 zip'lik bir teslimatı 7 zip diye anlatıyordu."
    )
    lines.append("")
    lines.append(f"- Snapshot: `{receipt['snapshot_id']}`")
    lines.append(f"- Commit: `{ident['repo_commit'] or 'yok (git worktree değil)'}`"
                 f"{' — **dirty**' if ident['dirty'] else ''}")
    lines.append(f"- Export: `{receipt['export_started_utc']}` → `{receipt['export_finished_utc']}` (UTC)")
    lines.append(f"- Handoff bütünlük kapısı: **{receipt['verdict']}**")
    lines.append(f"- Üçüncü tarafa yükleme yetkisi: "
                 f"**{'VAR' if receipt['upload_authorized'] else 'YOK'}** "
                 f"— {receipt['disclosure_gate']['reason']}")
    lines.append("")
    lines.append(
        "> Bu verdict **PCB tasarımı hakkında değildir.** Yalnız şunu söyler: bu "
        "snapshot, beyan edilen kapsamı bu hash'lerle taşıyor."
    )
    lines.append("")
    lines.append("## Paketler")
    lines.append("")
    lines.append("| Zip | İçerik | Dosya | Metin | Zip |")
    lines.append("|---|---|---:|---:|---:|")
    for b in receipt["bundles"]:
        m = manifests[b["name"]]
        lines.append(
            f"| `{b['zip_name']}` | {m['bundle_purpose']} | "
            f"{m['coverage_files']['scanned']} | "
            f"{human_bytes(m['coverage_bytes']['scanned'])} | "
            f"{human_bytes(b['zip_bytes'])} |"
        )
    lines.append("")
    agg = receipt["aggregate_coverage_files"]
    aggb = receipt["aggregate_coverage_bytes"]
    lines.append("## Kapsam aritmetiği")
    lines.append("")
    lines.append("```")
    lines.append(f"dosya : eligible {agg['eligible']} = scanned {agg['scanned']} "
                 f"+ excluded {agg['excluded']}   (violations {agg['violations']})")
    lines.append(f"bayt  : eligible {aggb['eligible']} = scanned {aggb['scanned']} "
                 f"+ excluded {aggb['excluded']}   (violations {aggb['violations']})")
    lines.append("```")
    lines.append("")
    lines.append(
        "Her paketin kökündeki `MANIFEST.json` aynı aritmetiği paket düzeyinde, "
        "dosya başına SHA-256 ve dışlama envanteriyle birlikte taşır."
    )
    lines.append("")
    indexed = [b["name"] for b in receipt["bundles"] if manifests[b["name"]]["derived_views"]]
    if indexed:
        lines.append(
            f"`EVIDENCE_INDEX.json` ({', '.join(indexed)}): kanıt JSON'larının "
            f"birebir kopyalanmış üst düzey alanları, her satırda backing dosya "
            f"+ SHA-256 ile. **`authority: screen_only`** — kanıtı *bulmak* için, "
            f"kanıt *yerine* değil. Bir kapıyı kapatmaz; bir satıra dayanmadan "
            f"önce backing dosyayı yeniden oku."
        )
        lines.append("")
    lines.append("## Dışarıda bırakılanlar")
    lines.append("")
    lines.append("| Örüntü | Politika kaynağı | Neden | Dosya | Bayt |")
    lines.append("|---|---|---|---:|---:|")
    for exc in receipt["exclusion_inventory"][:20]:
        lines.append(
            f"| `{exc['pattern']}` | {exc['policy_source']} | {exc['reason']} | "
            f"{exc['count']} | {human_bytes(exc['bytes'])} |"
        )
    if len(receipt["exclusion_inventory"]) > 20:
        lines.append(f"| … | | {len(receipt['exclusion_inventory']) - 20} örüntü daha "
                     f"(tam liste `handoff_receipt.json`) | | |")
    lines.append("")
    lines.append("## Taramalar")
    lines.append("")
    cs = receipt["credential_scan"]
    lines.append(f"- `credential_scan`: {cs['coverage_files']['scanned']} dosya tarandı, "
                 f"{len(cs['hard_hits'])} canlı-görünümlü kimlik bilgisi, "
                 f"{cs['soft_hit_count']} zayıf eşleşme. **Bu tarama yalnız kimlik "
                 f"bilgisi biçimlerini ölçer; korpusun üçüncü tarafa çıkmaya yetkili "
                 f"olduğunu SÖYLEMEZ** — o ayrı bir kapı (`disclosure_gate`).")
    cp = receipt["content_policy"]
    lines.append(f"- `content_policy`: {cp['coverage_files']['scanned']} dosya, "
                 f"{cp['coverage_files']['violations']} ihlal. "
                 f"`file:` ile eklenen dosyalar da bu taramadan geçer.")
    dg = receipt["disclosure_gate"]
    lines.append(
        f"- `disclosure_gate`: **{dg['status']}** — {dg['allowed']} allow, "
        f"{dg['confirm_required']} confirm"
        f"{' (' + '/'.join(dg['confirm_classes']) + ')' if dg['confirm_classes'] else ''}, "
        f"{len(dg['denied'])} deny, {dg['unclassified']} sınıflandırılmamış. "
        f"Otorite: {(dg['authority'] or 'tanımsız').rstrip('.')}."
    )
    lines.append(f"  {dg['reason']}")
    lines.append("")
    lines.append("## Yeniden üretmek")
    lines.append("")
    lines.append("```bash")
    lines.append(f"BUNDLES_CONF={receipt['bundles_conf'] or '<yok>'} \\")
    lines.append(f"PROJECT_ROOT={receipt['project_root']} \\")
    lines.append(f"OUTPUT_DIR={receipt['output_dir']} \\")
    lines.append(f"BUNDLE_PREFIX={receipt['bundle_prefix']} \\")
    entry = next((t for t in receipt["export_tool_files"] if t["name"] == "export.sh"),
                 None)
    lines.append(f"bash {entry['path'] if entry else '<export.sh>'}")
    lines.append("```")
    lines.append("")
    lines.append(
        "Tazelik kontrolü (`gptpro-handoff` adım 3): `bash <export.sh> --recheck` "
        "canlı HEAD'i bu makbuzdakiyle karşılaştırır ve ayrışma varsa `STALE` der."
    )
    lines.append("")
    return "\n".join(lines)


def human_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)}B"
    for unit in ("K", "M", "G"):
        n /= 1024.0
        if n < 1024 or unit == "G":
            return f"{n:.1f}{unit}"
    return f"{n:.1f}G"


# --------------------------------------------------------------------------
# main export
# --------------------------------------------------------------------------

def run_export(plan: Plan) -> int:
    started = utc_now()
    here = Path(__file__).resolve().parent
    tool_files = []
    for name in ("export.sh", "gptpro_export.py"):
        p = here / name
        if p.is_file():
            tool_files.append({"path": str(p), "name": name, "sha256": sha256_file(p)})
    export_tool_sha = sha256_text(
        "\n".join(f"{t['name']}:{t['sha256']}" for t in tool_files)
    )
    bundles_conf_sha = sha256_file(plan.bundles_conf) if plan.bundles_conf else None
    identity = snapshot_identity(plan.project_root)

    log(f"project: {plan.project_root}")
    log(f"output:  {plan.output_dir}")
    log(f"commit:  {identity['repo_commit'] or 'n/a'}"
        f"{'  (DIRTY)' if identity['dirty'] else ''}")

    work = Path(tempfile.mkdtemp(prefix="gptpro-export-",
                                 dir=os.environ.get("TMPDIR", "/tmp")))
    staging_root = work / "staging"
    staging_root.mkdir(parents=True)

    # The finished set is assembled INSIDE OUTPUT_DIR, not in TMPDIR. The publish
    # step is a rename, and a rename only works within one filesystem — with
    # /tmp on tmpfs and the repo on disk, building it in TMPDIR would fail at the
    # last step, after all the work.
    plan.output_dir.mkdir(parents=True, exist_ok=True)
    publish_tmp = Path(tempfile.mkdtemp(prefix=".publish-", dir=plan.output_dir))

    try:
        results = [stage_bundle(plan, b, staging_root) for b in plan.bundles]
        for res in results:
            log(f"staged {res['bundle'].name}: {len(res['staged'])} files, "
                f"{len(res['excluded'])} excluded")

        # --- required-source gate. Explicit config: a missing source is lost
        # evidence, not a layout variation. There is no ALLOW_MISSING.
        missing = [
            (res["bundle"].name, decl)
            for res in results for decl in res["missing_required"]
        ]
        if missing:
            for name, decl in missing:
                warn(f"required source missing for bundle {name!r}: {decl}")
            raise Fatal(
                V_NO_COVERAGE,
                f"{len(missing)} required source(s) missing. The eligibility universe "
                f"for them cannot be measured, so their coverage is unknown rather "
                f"than zero. Fix the path, or declare the source `optional:` so the "
                f"decision is visible in the manifest.",
            )

        empty = [res["bundle"].name for res in results if not res["staged"]]
        if empty:
            raise Fatal(V_NO_COVERAGE,
                        f"bundle(s) staged nothing: {empty}. scanned=0 is NO_COVERAGE.")

        scan = scan_staged(results, staging_root, plan.allow_secrets)

        if scan["content_policy"]["violations"]:
            for v in scan["content_policy"]["violations"][:25]:
                warn(f"restricted class in {v['bundle']}: {v['repo_path']} ({v['class']}) "
                     f"via {v['source_decl']}")
            raise Fatal(
                V_RED,
                f"{len(scan['content_policy']['violations'])} staged file(s) belong to a "
                f"restricted class. A `file:` declaration is an inclusion mechanism, not "
                f"a disclosure decision — it does not exempt a file from this policy.",
            )

        hard = scan["credential_scan"]["hard_hits"]
        if hard:
            for h in hard[:25]:
                warn(f"live-looking credential: {h['bundle']}:{h['repo_path']}:{h['line']}")
            if not plan.allow_secrets:
                raise Fatal(V_RED,
                            f"{len(hard)} live-looking credential hit(s). Remove the "
                            f"values, or re-run with ALLOW_SECRETS=1 if every hit above "
                            f"is a fixture (the run can then never be GREEN).")
            warn("ALLOW_SECRETS=1 — continuing; this run can no longer be GREEN.")

        disclosure = disclosure_gate(plan.disclosure_policy, results)
        if disclosure["status"] == V_RED:
            for d in disclosure["denied"][:25]:
                warn(f"disclosure denied: {d['bundle']}:{d['repo_path']} "
                     f"({d['class']}) — {d['basis']}")
            raise Fatal(
                V_RED,
                f"{len(disclosure['denied'])} staged file(s) are denied disclosure by "
                f"{disclosure['authority']}. A policy breach is a build failure, not a "
                f"footnote on a published set: nothing is written.",
            )

        finished = utc_now()
        snapshot_seed = "|".join([
            identity["repo_commit"] or "no-commit",
            identity["dirty_inventory_sha256"] or "clean",
            bundles_conf_sha or "autodetect",
            export_tool_sha,
        ])
        snapshot_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + sha256_text(snapshot_seed)[:12]
        )

        ctx = {
            "staging_root": staging_root,
            "snapshot_id": snapshot_id,
            "identity": identity,
            "started_utc": started,
            "finished_utc": finished,
            "bundles_conf_sha256": bundles_conf_sha,
            "export_tool_sha256": export_tool_sha,
            "export_tool_files": tool_files,
            "scan": scan,
            "derived_views": {},
        }

        engine_sha = next(
            (t["sha256"] for t in tool_files if t["name"] == "gptpro_export.py"),
            export_tool_sha,
        )
        for res in results:
            index = build_evidence_index(res, staging_root, engine_sha)
            if index is None:
                continue
            index_path = staging_root / res["bundle"].name / "EVIDENCE_INDEX.json"
            write_json(index_path, index)
            ctx["derived_views"][res["bundle"].name] = [{
                "path": "EVIDENCE_INDEX.json",
                "sha256": sha256_file(index_path),
                "generator": GENERATOR,
                "generator_sha256": engine_sha,
                "derived_from": [row["derived_from"][0] for row in index["rows"]],
                "authority": "screen_only",
            }]
            log(f"indexed {res['bundle'].name}: {len(index['rows'])} evidence "
                f"documents (screen_only)")

        manifests: dict[str, dict] = {}
        problems: list[str] = []
        for res in results:
            m = build_manifest(res, ctx, results)
            manifests[res["bundle"].name] = m
            problems.extend(check_coverage(m, res["bundle"].name))
        if problems:
            for p in problems:
                warn(p)
            raise Fatal(V_INVALID_COVERAGE,
                        "coverage arithmetic does not close; see the lines above")

        # --- zip, then verify each archive against the manifest it carries.
        bundle_entries = []
        for res in results:
            name = res["bundle"].name
            m = manifests[name]
            write_json(staging_root / name / "MANIFEST.json", m)
            manifest_sha = sha256_file(staging_root / name / "MANIFEST.json")

            zip_name = f"{plan.bundle_prefix}-{name}.zip"
            zip_path = publish_tmp / zip_name
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                root = staging_root / name
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        zf.write(path, f"{name}/{path.relative_to(root).as_posix()}")

            mismatches = verify_zip(zip_path, name, m)
            if mismatches:
                for mm in mismatches[:25]:
                    warn(f"zip/manifest mismatch in {name}: {mm}")
                raise Fatal(V_RED,
                            f"{len(mismatches)} file(s) in {zip_name} do not match the "
                            f"manifest they ship with")

            bundle_entries.append({
                "name": name,
                "purpose": res["bundle"].desc,
                "zip_name": zip_name,
                "zip_bytes": zip_path.stat().st_size,
                "zip_sha256": sha256_file(zip_path),
                "manifest_sha256": manifest_sha,
                "files": m["coverage_files"]["scanned"],
                "bytes": m["coverage_bytes"]["scanned"],
            })
            log(f"packed {zip_name} ({human_bytes(bundle_entries[-1]['zip_bytes'])})")

        receipt = build_receipt(plan, ctx, results, manifests, bundle_entries,
                                scan, disclosure)
        # Belt and braces: every RED path above raises before reaching here. If a
        # future condition computes RED without raising, publishing it anyway
        # would put a set on disk that the receipt itself calls a failure.
        if receipt["verdict"] == V_RED:
            raise Fatal(V_RED, "; ".join(receipt["verdict_reasons"]))

        write_json(publish_tmp / "handoff_receipt.json", receipt)
        guide = render_guide(receipt, manifests)
        (publish_tmp / "00-OKUNACAK.md").write_text(guide, encoding="utf-8")

        run_dir = publish(plan, publish_tmp, snapshot_id)

        log(f"published: {run_dir}")
        log(f"verdict:   {receipt['verdict']}  "
            f"(upload authorized: {receipt['upload_authorized']})")
        for note in receipt["verdict_reasons"]:
            log(f"  - {note}")
        return 0

    finally:
        shutil.rmtree(work, ignore_errors=True)
        # On success this was renamed away and no longer exists; on failure it
        # holds a half-built set that must never be mistaken for a published one.
        shutil.rmtree(publish_tmp, ignore_errors=True)


def verify_zip(zip_path: Path, bundle_name: str, manifest: dict) -> list[str]:
    """Read the archive back and re-hash it. A manifest that was never checked
    against the thing it describes is a promise, not a receipt."""
    problems = []
    expected = {f["path"]: (f["sha256"], f["size_bytes"]) for f in manifest["files"]}
    # MANIFEST.json cannot hash itself; the derived views are hashed in
    # derived_views[] instead of files[] so they stay out of the coverage
    # arithmetic, which counts staged SOURCE files only.
    generated = {"MANIFEST.json"} | {v["path"] for v in manifest["derived_views"]}
    with zipfile.ZipFile(zip_path) as zf:
        names = set()
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename
            prefix = f"{bundle_name}/"
            if not rel.startswith(prefix):
                problems.append(f"unexpected archive member {rel}")
                continue
            rel = rel[len(prefix):]
            if rel in generated:
                continue
            names.add(rel)
            if rel not in expected:
                problems.append(f"{rel} is in the zip but not in the manifest")
                continue
            with zf.open(info) as fh:
                h = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
                    size += len(chunk)
            want_sha, want_size = expected[rel]
            if h.hexdigest() != want_sha or size != want_size:
                problems.append(f"{rel}: hash/size differs from manifest")
        for rel in expected:
            if rel not in names:
                problems.append(f"{rel} is in the manifest but not in the zip")
    return problems


def build_receipt(plan, ctx, results, manifests, bundle_entries, scan, disclosure) -> dict:
    agg_f = {"eligible": 0, "scanned": 0, "excluded": 0, "violations": 0}
    agg_b = {"eligible": 0, "scanned": 0, "excluded": 0, "violations": 0}
    for m in manifests.values():
        for key in agg_f:
            agg_f[key] += m["coverage_files"][key]
            agg_b[key] += m["coverage_bytes"][key]

    all_exclusions = exclusion_inventory(
        [rec for res in results for rec in res["excluded"]]
    )

    maker_id = os.environ.get("GPTPRO_MAKER_ID") or None
    checker_id = os.environ.get("GPTPRO_CHECKER_ID") or None
    separate = bool(maker_id and checker_id and maker_id != checker_id)
    if separate:
        default_note = (
            "maker and checker identifiers differ and were supplied by the operator "
            "at export time; this records the claim, it does not verify it"
        )
    elif maker_id or checker_id:
        default_note = (
            f"maker_id and checker_id are the same identity ({maker_id or checker_id}); "
            f"that is a role split inside one context, not an independent check, and "
            f"shared-assumption error stays unmeasured"
        )
    else:
        default_note = (
            "no maker_id/checker_id supplied at export time; role separation is "
            "unmeasured, so it must be stated rather than implied away"
        )
    independence_note = os.environ.get("GPTPRO_INDEPENDENCE_NOTE") or default_note

    verdict = V_GREEN
    reasons: list[str] = []

    if ctx["identity"]["vcs"] is None:
        verdict = V_CONFIRM
        reasons.append("PROJECT_ROOT is not a git worktree — the snapshot has no "
                       "verifiable identity to be stale against")
    elif ctx["identity"]["dirty"]:
        verdict = V_CONFIRM
        reasons.append(
            f"working tree is dirty ({ctx['identity']['tracked_modified']} tracked "
            f"modified, {ctx['identity']['untracked']} untracked). Whether a dirty "
            f"tree may be handed off is spec open question K-3 and is undecided, so "
            f"this run takes the conservative reading and does not go GREEN. The "
            f"drift is pinned by dirty_inventory_sha256."
        )

    if disclosure["status"] == V_RED:
        verdict = V_RED
        reasons.append(disclosure["reason"])
    elif disclosure["status"] != V_GREEN:
        verdict = V_RED if verdict == V_RED else V_CONFIRM
        reasons.append(f"disclosure gate: {disclosure['reason']}")

    if scan["credential_scan"]["allow_secrets_override"]:
        verdict = V_RED if verdict == V_RED else V_CONFIRM
        reasons.append("ALLOW_SECRETS=1 was used to pass live-looking credential hits")

    if verdict == V_GREEN:
        reasons.append("declared scope fully staged, coverage arithmetic closes, no "
                       "missing required source, manifest hashes match the archives, "
                       "disclosure gate closed")

    upload_authorized = verdict == V_GREEN and disclosure["upload_authorized"]

    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR,
        "snapshot_id": ctx["snapshot_id"],
        "project_root": str(plan.project_root),
        "output_dir": str(plan.output_dir),
        "bundle_prefix": plan.bundle_prefix,
        "bundles_conf": str(plan.bundles_conf) if plan.bundles_conf else None,
        "bundles_conf_sha256": ctx["bundles_conf_sha256"],
        "export_tool_sha256": ctx["export_tool_sha256"],
        "export_tool_files": ctx["export_tool_files"],
        "config_mode": plan.mode,
        "export_started_utc": ctx["started_utc"],
        "export_finished_utc": ctx["finished_utc"],
        "snapshot": ctx["identity"],
        "authority": {
            "asserts": "this snapshot carries the declared scope at these hashes",
            "does_not_assert": [
                "any judgement about the PCB design",
                "that an external model's finding is true (that is claim-audit)",
                "the level/coverage/authority class of any rule "
                "(that is dogrulama-matrisi)",
            ],
        },
        "bundles": bundle_entries,
        "aggregate_coverage_files": agg_f,
        "aggregate_coverage_bytes": agg_b,
        "exclusion_inventory": all_exclusions,
        "missing_sources": [
            {"bundle": res["bundle"].name, "decl": decl}
            for res in results for decl in res["missing_required"]
        ],
        "optional_sources_absent": [
            {"bundle": name, "decl": decl}
            for name, m in manifests.items() for decl in m["optional_sources_absent"]
        ],
        "credential_scan": scan["credential_scan"],
        "content_policy": scan["content_policy"],
        "disclosure_gate": disclosure,
        "maker_id": maker_id,
        "checker_id": checker_id,
        "independence": {
            "separate_session": separate,
            "note": independence_note,
        },
        "external_report_acceptance": (
            V_CONFIRM if not separate else "eligible_for_claim_audit"
        ),
        "verdict": verdict,
        "verdict_reasons": reasons,
        "upload_authorized": upload_authorized,
        "verdict_scope": (
            "handoff integrity only. This is NOT a statement about the PCB design."
        ),
    }


def publish(plan: Plan, publish_tmp: Path, snapshot_id: str) -> Path:
    """Atomic bundle-set publish.

    The old script deleted the previous zips *before* building the new ones, so
    a run that died in the middle left a half set that looked like a whole one.
    Here a run is only ever visible complete: the set is built off to the side,
    moved into runs/<snapshot_id> in one rename, and only then does `current`
    point at it.
    """
    runs = plan.output_dir / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    run_dir = runs / snapshot_id
    if run_dir.exists():
        shutil.rmtree(run_dir)
    os.replace(publish_tmp, run_dir)

    current = plan.output_dir / "current"
    if current.exists() and not current.is_symlink():
        raise Fatal(
            V_RED,
            f"{current} exists but is not a symlink; refusing to replace it. Move "
            f"it aside — the run is in {run_dir} and is complete.",
        )
    tmp_link = plan.output_dir / f".current.{os.getpid()}"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    os.symlink(f"runs/{snapshot_id}", tmp_link)
    os.replace(tmp_link, current)

    existing = sorted(
        (d for d in runs.iterdir() if d.is_dir()),
        key=lambda d: d.name, reverse=True,
    )
    for old in existing[plan.keep_runs:]:
        shutil.rmtree(old, ignore_errors=True)

    # Anything loose at the top of OUTPUT_DIR predates the run-id layout. It is
    # not deleted for you — that is the operator's call on their own files — but
    # it is named every run, because a stale guide sitting beside a fresh set is
    # the exact drift this rewrite exists to end. The old hand-written
    # 00-OKUNACAK.md described seven zips next to eight.
    stale = sorted(
        p for p in plan.output_dir.glob("*")
        if p.is_file() and not p.name.startswith(".")
    )
    if stale:
        warn(f"{len(stale)} file(s) sit loose in {plan.output_dir} from the "
             f"pre-run-id layout: {[p.name for p in stale][:8]}. They are NOT part "
             f"of this snapshot. Upload from {plan.output_dir}/current/ and remove "
             f"them once you are sure — a stale 00-OKUNACAK.md next to a fresh set "
             f"is worse than no guide at all.")
    return run_dir


# --------------------------------------------------------------------------
# freshness re-check
# --------------------------------------------------------------------------

def run_recheck(project_root: Path, run_dir: Path) -> int:
    receipt_path = run_dir / "handoff_receipt.json"
    if not receipt_path.is_file():
        print(f"[gptpro] ERROR: no handoff_receipt.json under {run_dir}", file=sys.stderr)
        return 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    was = receipt["snapshot"]
    now = snapshot_identity(project_root.resolve())

    same_commit = was["repo_commit"] == now["repo_commit"]
    same_dirty = was["dirty_inventory_sha256"] == now["dirty_inventory_sha256"]
    verdict = V_GREEN if (same_commit and same_dirty) else V_STALE

    log(f"snapshot:  {receipt['snapshot_id']}")
    log(f"exported:  commit {was['repo_commit']} dirty={was['dirty']}")
    log(f"live:      commit {now['repo_commit']} dirty={now['dirty']}")
    log(f"freshness: {verdict}")
    if verdict == V_STALE:
        if not same_commit:
            log("  - HEAD moved since the export")
        if not same_dirty:
            log("  - working-tree drift differs from the exported inventory")
        log("  - re-run the export and have the operator re-upload before treating "
            "the corpus as current")
    print(json.dumps({
        "snapshot_id": receipt["snapshot_id"],
        "exported": {"repo_commit": was["repo_commit"], "dirty": was["dirty"],
                     "dirty_inventory_sha256": was["dirty_inventory_sha256"]},
        "live": {"repo_commit": now["repo_commit"], "dirty": now["dirty"],
                 "dirty_inventory_sha256": now["dirty_inventory_sha256"]},
        "freshness": verdict,
    }, indent=2))
    return 0 if verdict == V_GREEN else 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan")
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--project-root")
    ap.add_argument("--run-dir")
    args = ap.parse_args(argv)

    if args.recheck:
        return run_recheck(Path(args.project_root or "."), Path(args.run_dir or "."))

    if not args.plan:
        ap.error("--plan is required (this engine is driven by export.sh)")

    try:
        plan = Plan(Path(args.plan))
        return run_export(plan)
    except Fatal as exc:
        print(f"\n[gptpro] {exc.verdict}: {exc}\n", file=sys.stderr)
        print("[gptpro] nothing published; the previous bundle set is untouched.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
