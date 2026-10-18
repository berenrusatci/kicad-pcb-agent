#!/usr/bin/env python3
"""Scans every module under a repo root and produces an auto-updated
"Capability Inventory" (YETENEK_ENVANTERI.xlsx + .csv) - answers "can this
tool actually do X" by reading a spreadsheet instead of manually re-reading
the repo.

WHY THIS EXISTS: as a project grows (70+ Python modules across two merged
repos), which module is FULLY functional, which is a SKELETON, and which is
actually tested stops fitting in anyone's head. This module produces that
analysis table FROM CODE, by reading the repo's real files.

HONESTY BOUNDARY (deliberate, and repeated on purpose):
This inventory does not make architectural decisions automatically - it
only gives a fast, current answer to "what exists, what doesn't, what's
half-done":
  - ``status`` (Var/Kısmen var/İskelet) is a ROUGH ESTIMATE based on
    function count/body length UNLESS the docstring carries an explicit
    marker - flagged with ``status_source="otomatik_tahmin"``, never
    claimed as a certain verdict.
  - ``real_or_mock`` only flags SUSPICIOUS patterns - a CONFESSION in the
    file's own prose ("is a stub", "a placeholder for the real API"), or an
    outward-facing function answering with canned constants. It never
    automatically CONDEMNS a module as mock, only flags "SHOULD BE CHECKED",
    and an unflagged module is NOT thereby certified real. Screened-out hits
    keep their reason and ship in the run report's ``mock_screen``, so the
    screens can be audited instead of trusted.
  - ``tool_chain_compat`` (Tam/Kısmen uyumlu, İlgisiz) is a ROUGH pattern
    match (json + loop), not a precise static analysis.
  - ``test_status`` is the ONE exception - it is REALLY measured with
    ``pytest --junit-xml`` (see ``run_all_tests_and_collect_results()``),
    never a made-up "all passed" default; a caller that wants to skip it
    must explicitly opt in with ``--skip-tests``.
  - The four AST-based columns added in this port (security status,
    internal/external dependencies, AI contract) are the SAME kind of
    pattern-based signal, not a guarantee - see each function's own
    docstring for exactly what it does and does not detect.

Before a critical decision ("can we actually use this feature"), the
relevant file may still need to be checked BY HAND - this is noted as a
reminder on the Summary sheet too.

Usage:
    capability_inventory.py <repo_root> [--out inventory.xlsx]
                            [--skip-tests] [--json out.json]
    capability_inventory.py --self-test

Exit codes: 0 = inventory written, 2 = bad input or run error (this tool
has no PASS/FAIL verdict of its own - it is a report generator).
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import re
import subprocess
import sys
import tempfile
import tokenize
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "capability-inventory-report/v1"
TOOL_NAME = "capability_inventory"

EXCLUDED_DIR_NAMES = {".git", ".venv", "venv", "__pycache__", ".scratch",
                      ".pytest_cache", "node_modules", "build", "dist"}


def turkish_lower(text: str) -> str:
    """``str.lower()`` maps Turkish upper-case ``İ`` to ``i`` + COMBINING
    DOT ABOVE (U+0307) (Unicode's locale-independent rule) - this silently
    breaks plain substring matching (measured: ``'DEĞİL'.lower()`` does NOT
    match ``'değil'``) and can also raise ``UnicodeEncodeError`` on a
    Windows console (cp125x). Converting ``İ``->``i``, ``I``->``ı`` (the
    correct Turkish lower-case mappings) BEFORE calling ``.lower()`` makes
    this trap structurally impossible."""
    return text.replace("İ", "i").replace("I", "ı").lower()


# --------------------------------------------------------------------------- #
# 1. Category classification - keyword matching
# --------------------------------------------------------------------------- #
# Order MATTERS: a file can match more than one category, the FIRST match
# wins (more SPECIFIC categories are listed FIRST).
CATEGORY_KEYWORDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("çoklu-kart", ("coklu_kart", "sistem_orkestratoru", "arayuz_sozlesmesi", "device_tree")),
    ("HDI", ("hdi_stackup", "via_siniflandirma", "mikrovia", "blind_buried")),
    ("empedans/SI", ("empedans", "impedance", "skew", "highspeed", "high_speed", "diferansiyel",
                      "diff_pair", "openems")),
    ("termal", ("termal", "isil", "sicaklik", "junction", "soguturucu", "thermal")),
    ("PDN/güç", ("pdn", "_guc_", "guc_butcesi", "power", "decoupling", "pmic", "ray_tahsisi",
                 "akim_yogunlugu", "current_density")),
    ("yerleşim/placement", ("yerlesim", "placement", "kumeleri_bul", "force_directed")),
    ("routing", ("router", "_rota_", "routing", "izgara_a_yildiz", "carpisma_radari",
                 "topolojik_router", "kurtarma_motoru", "diff_pair", "grid_astar", "safe_write")),
    ("DFM/üretim", ("dfm", "uretim_", "gerber", "kibot", "jlcpcb", "ipc7351", "ipc6012",
                     "ipc2152", "ipc2221", "ipc_dru", "footprint", "panelizasyon")),
    ("test/doğrulama", ("dogrulama", "verify", "_kontrolu", "kontrolu_", "drc", "erc",
                         "checker", "hata_hafizasi", "karar_birimleri", "error_memory")),
)


def determine_category(file_name: str, source: str) -> str:
    """Looks in the file NAME first, falls back to file CONTENT - returns
    "diğer" (other) if nothing matches, never an invented category."""
    name_lower = turkish_lower(file_name)
    for category, keywords in CATEGORY_KEYWORDS:
        if any(k in name_lower for k in keywords):
            return category
    source_lower = turkish_lower(source)
    for category, keywords in CATEGORY_KEYWORDS:
        if any(k in source_lower for k in keywords):
            return category
    return "diğer"


# --------------------------------------------------------------------------- #
# 2. Status detection - marker first, else a rough automatic estimate
# --------------------------------------------------------------------------- #
# DECLARATIVE PHRASES, not the bare word. "iskelet" on its own matches any
# module that merely MENTIONS skeletons - including this one, whose docstring
# has to name its own status vocabulary ("Var/Kısmen var/İskelet"), and any
# module whose prose explains that it is NOT a skeleton. A marker has to be
# the file SAYING WHAT IT IS. These phrases are the ones the archive's real
# skeleton modules actually used ("bilerek bir İSKELET + YER TUTUCUDUR",
# "GENİŞLETİLEBİLİRLİK İSKELETİ").
_SKELETON_MARKERS = (
    "bir iskelet",
    "sadece iskelet",
    "iskelettir",
    "iskeletidir",
    "iskeleti̇dir",
    "yalnızca iskelet",
    "yalnizca iskelet",
)
_PARTIAL_MARKERS = ("tam uygulama değil", "tam uygulama degil", "kapsam dışı", "kapsam disi")


def determine_status(source: str, path: Path | None = None) -> Tuple[str, str]:
    """Returns ``(status, status_source)``.

    ``status_source``:
      * ``"marker"`` - the module's OWN DOCSTRING carried an explicit marker
        phrase. This is the reliable signal.
      * ``"dosya_turu"`` - the file is a package marker or a test fixture,
        i.e. it is complete BY DESIGN and has nothing to implement.
      * ``"otomatik_tahmin"`` - a ROUGH estimate from function count/length
        (needs manual confirmation).

    THE MARKER SCAN IS DOCSTRING-ONLY AND PHRASE-BASED, ON PURPOSE. Two
    separate defects made this file flag ITSELF as a skeleton:
      1. the scan read the WHOLE source, so the marker list's own
         definition line matched;
      2. the marker was the BARE WORD "iskelet", so even a docstring-only
         scan still matched this module - its docstring has to name its own
         status vocabulary ("Var/Kısmen var/İskelet").
    A marker now has to be the file SAYING WHAT IT IS ("bir iskelet",
    "sadece iskelet", ...), read from the module docstring only.

    PACKAGE MARKERS AND FIXTURES ARE NOT SKELETONS. A zero-function
    ``__init__.py`` is complete: there is nothing to implement. Calling it a
    skeleton produced 12 false "unfinished work" entries out of 18 and hid
    the one real scaffold in the noise.
    """
    if path is not None:
        parts = Path(path).parts
        if Path(path).name == "__init__.py":
            return "Paket işaretçisi", "dosya_turu"
        if "fixtures" in parts or "tests" in parts:
            return "Test altyapısı", "dosya_turu"

    try:
        docstring = ast.get_docstring(ast.parse(source)) or ""
    except SyntaxError:
        docstring = ""
    docstring_lower = turkish_lower(docstring)
    if any(m in docstring_lower for m in _SKELETON_MARKERS):
        return "İskelet", "marker"
    if any(m in docstring_lower for m in _PARTIAL_MARKERS):
        return "Kısmen var", "marker"

    top_level_defs = len(re.findall(r"^(?:def|class)\s+\w+", source, re.M))
    total_lines = len(source.splitlines())

    if top_level_defs <= 2 and total_lines < 80:
        return "İskelet", "otomatik_tahmin"
    if total_lines < 150:
        return "Kısmen var", "otomatik_tahmin"
    return "Var", "otomatik_tahmin"


# --------------------------------------------------------------------------- #
# 3. Summary extraction - the docstring's first 1-2 sentences
# --------------------------------------------------------------------------- #
def extract_summary(source: str) -> str:
    try:
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree)
    except SyntaxError:
        return "(docstring okunamadı - sözdizimi hatası)"
    if not docstring:
        return "(docstring yok)"

    # Skip title/separator lines (like ===...===) and blank lines, find the
    # first paragraph where real prose starts.
    paragraphs = [p.strip() for p in docstring.split("\n\n") if p.strip()]
    for p in paragraphs:
        clean = " ".join(
            line for line in p.splitlines()
            if line.strip() and not re.fullmatch(r"[=\-]{3,}", line.strip())
        ).strip()
        if clean:
            sentences = re.split(r"(?<=[.!?])\s+", clean)
            return " ".join(sentences[:2])[:400]
    return "(docstring yalnızca başlık/ayraç içeriyor)"


# --------------------------------------------------------------------------- #
# 4. Real/Mock suspicious pattern scan
# --------------------------------------------------------------------------- #
# A canned constant is only suspicious where a REAL answer would have to come
# from outside the process. Unscoped, this signal fires on every dispatch
# table - measured: all five modules it flagged were classifiers whose return
# genuinely depends on the input (file suffix -> kicad-cli subcommand, status
# -> the 0/1/2 exit contract, net name -> net class). So the enclosing
# function must both LOOK outward-facing and make no outward call.
_EXTERNAL_HINT = re.compile(
    r"\b(?:api|fetch|request|response|query|download|upload|endpoint|url|uri|"
    r"client|remote|submit|lookup|sunucu|istek|yanit|yanıt)\b", re.I)
_OUTWARD_CALL_NAMES = ("subprocess", "urllib", "requests", "httpx", "socket",
                       "http", "open", "urlopen", "run", "check_output", "read_text")

# CONFESSION PHRASES, not bare keywords - the same correction ``determine_status``
# already had to make for "iskelet" (see ``_SKELETON_MARKERS``). The bare-word
# scan flagged 38 of 98 modules; MEASURED against the tree, almost none were
# mocks:
#   * "stub" is a PHYSICAL object in this domain - the unterminated barrel of a
#     via (``via_stub.py``, ``STUB_LENGTH_THRESHOLD_MM``) and the short wire
#     leaving a schematic pin (``sch_wire.STUB``). 117 of the 199 keyword hits
#     were that word, plus "stubborn" matched as a SUBSTRING;
#   * "placeholder" is a DATA operation here - ``error_memory`` replaces
#     per-run values with placeholders BEFORE hashing, and
#     ``research_promotion_gate`` DETECTS placeholders in other people's docs;
#   * "hardcode" appeared almost exclusively inside its own denial
#     ("there is deliberately NO hardcoded fallback", "NEITHER is hardcoded").
# A confession is the file saying what ITS OWN code does. Everything below is
# adjacent-form on purpose: "is a stub" confesses, "is signal-integrity/stub
# focused" does not.
_CONFESSION_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("mock/fake", re.compile(
        r"\b(?:is|are|was|were)\s+(?:just\s+|only\s+|merely\s+|deliberately\s+|a\s+|an\s+)*"
        r"(?:mock|fake|sahte|taklit)\w*\b", re.I)),
    ("mock/fake", re.compile(
        r"\b(?:mock|fake|sahte)\s+(?:data|value|result|response|veri|değer|deger|sonuç|sonuc)\b",
        re.I)),
    ("mock/fake", re.compile(r"\breturns?\s+(?:a\s+)?(?:mock|fake)\w*\b", re.I)),
    ("stub", re.compile(
        r"\b(?:is|as|just|only|merely)\s+(?:a\s+|an\s+|the\s+)?stubs?\b|"
        r"\bstub\s+implementation\b|\bstubbed\s+out\b|"
        r"\bsadece\s+stub\b|\bstub\s+olarak\b", re.I)),
    ("placeholder", re.compile(
        r"\b(?:is|just|only|merely)\s+(?:a\s+|an\s+|the\s+)?placeholders?\b|"
        r"\bas\s+(?:a|an|the)\s+[\w\s-]{0,24}placeholders?\b|"
        r"\ba\s+placeholder\s+for\b|\bplaceholder\s+implementation\b|"
        r"\byer\s*tutucu\w*\b", re.I)),
    # "hardcode" needs no confession FORM: as a verb or adjective it always
    # describes code. What it does NOT say by itself is WHOSE code - hence the
    # negation and past-referent screens below carry this one.
    ("hardcode", re.compile(r"\bhard[\s-]?cod(?:e|es|ed|ing)\b", re.I)),
)

# A negator only counts when it GOVERNS the keyword, i.e. sits immediately in
# front of it inside the same clause. A clause-wide test was tried first and
# is wrong: it swallows ``jlcpcb_dfm``'s real confession
# ("GUESSED SHAPE, not verified: ... a placeholder for however the real API
# wants the file transferred") because an unrelated "not" appears earlier in
# the line.
_NEGATION_WINDOW_CHARS = 32
_NEGATOR = re.compile(
    r"\b(?:no|not|never|neither|nor|without|non|no\s+longer|rather\s+than|"
    r"instead\s+of|asla|hiçbir|hicbir|olmadan|yerine)\b\s*[\w\s,'\"`-]{0,%d}$" % _NEGATION_WINDOW_CHARS,
    re.I)
# Turkish puts its negator AFTER the word: "hardcoded değil".
_TRAILING_NEGATOR = re.compile(r"^\s*[\w\s,'\"`-]{0,16}\b(?:değil|degil|yok|yoktur)\b", re.I)

# A sentence that names another artefact is describing THAT artefact. Every
# "hardcode" hit outside jlcpcb_dfm was of this shape: the vendored shell
# script, the archive's monolithic main.py, the module this one replaced.
_PAST_REFERENT = re.compile(
    r"\b(?:archives?|archived|arşiv\w*|arsiv\w*|original\w*|orijinal\w*|vendored|"
    r"legacy|predecessor|previously|used\s+to|no\s+longer|the\s+old|eski|önceki|onceki)\b",
    re.I)
_OTHER_SOURCE_FILE = re.compile(r"\b[\w./\\-]+\.(?:py|sh)\b")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\(?\d+[.)])\s")


@dataclass
class MockScan:
    """The verdict plus BOTH sides of the evidence.

    ``suppressed`` exists because a filter that silently drops 199 hits is
    indistinguishable from a filter that is broken - the same failure mode
    that hid the junit key bug. Every discarded hit keeps its reason so the
    screens themselves can be audited from the run report.
    """
    verdict: str
    findings: List[str] = field(default_factory=list)
    suppressed: List[str] = field(default_factory=list)


def _canned_constant_return_functions(source: str) -> List[str]:
    """Functions that answer an OUTWARD-looking question with canned
    constants and never actually reach outside.

    Two or more ``if <test>: return <literal>`` branches, in a function whose
    name or parameters mention an API/request/endpoint, whose body contains no
    subprocess/urllib/socket/file call. A classifier keyed on its own argument
    fails the first screen; a bridge that "answers" without asking anyone
    fails none of them.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    flagged: List[str] = []
    for func in _function_nodes(tree):
        # The FUNCTION NAME only. Parameter names were tried and are too weak:
        # ``suggest_shove(blocker, request, ...)`` takes a local RouteRequest
        # dataclass and has nothing to do with a network request.
        # Underscores are word characters to ``re``, so ``\bapi\b`` does NOT
        # match inside ``fetch_api_price`` - split them into separators first.
        if not _EXTERNAL_HINT.search(func.name.replace("_", " ")):
            continue
        constant_branches = 0
        reaches_out = False
        for node in ast.walk(func):
            if isinstance(node, ast.If):
                body = [s for s in node.body if not isinstance(s, ast.Pass)]
                # ``return None`` is a REFUSAL - the opposite of fabricating an
                # answer, and the shape every guard clause in this bundle uses.
                # Only a value posing as data counts.
                if (len(body) == 1 and isinstance(body[0], ast.Return)
                        and isinstance(body[0].value, ast.Constant)
                        and body[0].value.value is not None):
                    constant_branches += 1
            elif isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if name in _OUTWARD_CALL_NAMES:
                    reaches_out = True
        if constant_branches >= 2 and not reaches_out:
            flagged.append(func.name)
    return flagged


def _self_test_line_ranges(tree: ast.AST) -> List[Tuple[int, int]]:
    """Line spans of ``*self_test*`` functions and the ``__main__`` block.

    Same rationale as excluding ``test_*.py`` from this scan entirely
    (``find_module_files``): a deterministic fake inside a self-test is the
    test double, not the module's production behaviour. ``openems._FakeBoard``
    and ``ai_feedback_loop``'s ``fake_sim`` are both of this kind.
    """
    ranges: List[Tuple[int, int]] = []
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "self_test" in node.name):
            ranges.append((node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.If):
            test = ast.dump(node.test)
            if "__name__" in test and "__main__" in test:
                ranges.append((node.lineno, node.end_lineno or node.lineno))
    return ranges


def _prose_blocks(source: str) -> List[Tuple[str, int]]:
    """(text, first line) for every docstring and comment OUTSIDE self-test
    blocks.

    PROSE ONLY, deliberately. A keyword inside an identifier is a NAME, not a
    claim: ``via_stub``, ``stub_length_mm``, ``mock_findings`` and this
    module's own ``_SUSPICIOUS_KEYWORDS`` list were all flagged by the old
    whole-source scan. The claim "this module is a mock" is always made in
    prose - and the CODE side is not left uncovered, it is what
    ``determine_security_status`` (trivial body + confession) reads.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    skip = _self_test_line_ranges(tree)

    def outside(line: int) -> bool:
        return not any(lo <= line <= hi for lo, hi in skip)

    blocks: List[Tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node)
        if not doc:
            continue
        line = getattr(node, "lineno", 1)
        if outside(line):
            blocks.append((doc, line))

    # Consecutive comment lines are ONE block. Split per line, a quotation
    # that opens on one line and closes on the next stops looking like a
    # quotation, and the citation screen below cannot see it.
    run: List[str] = []
    run_start = 0
    previous_line = -2

    def flush() -> None:
        if run:
            blocks.append(("\n".join(run), run_start))
            run.clear()

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                continue
            if not outside(tok.start[0]):
                continue
            if tok.start[0] != previous_line + 1:
                flush()
                run_start = tok.start[0]
            run.append(tok.string.lstrip("#").strip())
            previous_line = tok.start[0]
        flush()
    except (tokenize.TokenError, IndentationError, SyntaxError):
        flush()  # comments are a bonus; docstrings already carry the claims
    return blocks


def _paragraphs_with_context(text: str) -> List[Tuple[str, str]]:
    """(paragraph, context) where a bullet list inherits the paragraph that
    introduces it.

    ``drc_gate``'s docstring says "VENDORED FROM ...drc.sh. That script had
    four defects:" and then numbers them; defect 4 mentions a hardcoded path.
    Read alone that item looks like a confession by this file, so the lead-in
    travels with the list.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[Tuple[str, str]] = []
    for i, para in enumerate(paragraphs):
        context = ""
        if i and _LIST_ITEM.match(para) and paragraphs[i - 1].rstrip().endswith(":"):
            context = paragraphs[i - 1]
        out.append((para, context))
    return out


def _is_quoted(text: str, position: int) -> bool:
    """True when ``position`` sits inside a quotation.

    Quoting a phrase is CITING it, not asserting it - the distinction this
    module has already had to make twice about its own file (see
    ``_SKELETON_MARKERS``: naming a vocabulary is not a declaration). Without
    it, the file explaining that '"is a stub" confesses' confesses.
    """
    before = text[:position]
    for quote in ('"', "'"):
        if before.count(quote) % 2 == 1:
            return True
    return before.count("``") % 2 == 1


def scan_real_or_mock_detailed(source: str, module_name: str = "") -> MockScan:
    """"KONTROL EDİLMELİ" only when the file's own prose CONFESSES, in a
    sentence that is neither a denial nor about another artefact.

    Still not a verdict, and still not a claim that unflagged code is real -
    it is a narrower reading of the same weak signal. What changed is the
    error direction: the previous scan raised its hand on 38 of 98 modules,
    which is the same as raising it on none.
    """
    findings: List[str] = []
    suppressed: List[str] = []
    own_stem = Path(module_name).stem if module_name else ""

    for text, line in _prose_blocks(source):
        for para, context in _paragraphs_with_context(text):
            flat = " ".join(para.split())
            # A referent is a property of the PARAGRAPH, not of one clause: the
            # sentence naming the vendored script and the sentence describing
            # its defect are neighbours, and only the first one names it.
            scope = f"{context} {flat}"
            others = [f for f in _OTHER_SOURCE_FILE.findall(scope)
                      if Path(f).stem != own_stem]
            about_elsewhere = bool(_PAST_REFERENT.search(scope) or others)
            for clause in re.split(r"(?<=[.!?:;])\s+", flat):
                for label, pattern in _CONFESSION_PATTERNS:
                    for match in pattern.finditer(clause):
                        where = f"satır ~{line}: \"{clause[:90].strip()}\""
                        before = clause[max(0, match.start() - _NEGATION_WINDOW_CHARS):
                                        match.start()]
                        if _NEGATOR.search(before) or _TRAILING_NEGATOR.match(
                                clause[match.end():]):
                            suppressed.append(f"olumsuzlama [{label}] {where}")
                        elif _is_quoted(flat, flat.find(clause) + match.start()):
                            suppressed.append(f"alıntı [{label}] {where}")
                        elif about_elsewhere:
                            suppressed.append(f"başka artefakt [{label}] {where}")
                        else:
                            findings.append(f"[{label}] {where}")

    for func_name in _canned_constant_return_functions(source):
        findings.append(f"[sabit-yanıt] {func_name}(): dışarıya sormadan sabit döndürüyor")

    verdict = "KONTROL EDİLMELİ" if findings else "işaret yok (otomatik tarama)"
    return MockScan(verdict=verdict, findings=findings, suppressed=suppressed)


def scan_real_or_mock(source: str, module_name: str = "") -> Tuple[str, List[str]]:
    """``scan_real_or_mock_detailed`` without the suppression audit trail."""
    scan = scan_real_or_mock_detailed(source, module_name)
    return scan.verdict, scan.findings


# --------------------------------------------------------------------------- #
# 5. Combined tool-chain compatibility (draw -> simulate/compute -> JSON feedback)
# --------------------------------------------------------------------------- #
_JSON_PATTERN = re.compile(r"\bimport json\b|\bjson\.load\b|\bjson\.dump\b")
_LOOP_TARGET_PATTERN = re.compile(
    r"\bwhile\b[^\n]*\n(?:[^\n]*\n){0,15}?[^\n]*(?:hedef|target|yakinsa|converge|max_deneme|maks_iterasyon)",
    re.I,
)
_COMPUTE_DRAW_PATTERN = re.compile(
    r"\bpcbnew\b|\bhesapla\b|\bcoz\b|\brout[ei]\b|VECTOR2I|empedans",
    re.I,
)


def estimate_tool_chain_compat(source: str) -> str:
    has_json = bool(_JSON_PATTERN.search(source))
    has_loop = bool(_LOOP_TARGET_PATTERN.search(source))
    has_compute_draw = bool(_COMPUTE_DRAW_PATTERN.search(source))

    if has_json and has_loop and has_compute_draw:
        return "Tam uyumlu"
    if has_compute_draw or has_json:
        return "Kısmen uyumlu"
    return "İlgisiz"


# --------------------------------------------------------------------------- #
# 6. Security status (AST) - NEW in this port
# --------------------------------------------------------------------------- #
def _function_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _raises_notimplemented_or_exception(func_node) -> bool:
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(target, ast.Name) and target.id in ("NotImplementedError", "Exception"):
            return True
    return False


def _is_trivial_body(func_node) -> bool:
    """True if the function body is JUST ``pass``, a bare docstring, or a
    single ``return`` of a literal constant (``True``/``False``/a number/
    a string)."""
    body = [n for n in func_node.body if not isinstance(n, ast.Expr) or
           not isinstance(getattr(n, "value", None), ast.Constant)]  # drop a docstring-only Expr
    if not body:
        return True
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Return):
        return stmt.value is None or isinstance(stmt.value, ast.Constant)
    return False


def determine_security_status(source: str) -> str:
    """Classifies the module's functions/methods by failure behaviour:

    - "Güvenli İskelet (Fail-Closed)": at least one function body contains
      ``raise NotImplementedError`` or ``raise Exception`` - an
      unimplemented path that REFUSES rather than silently succeeding.
    - "Riskli Mock (Sessiz Hata)": at least one function has a TRIVIAL body
      (bare ``pass``, or a single ``return`` of a constant/``True``/
      ``False``) AND the file's own prose separately confesses
      (see ``scan_real_or_mock``) - a function that always returns the same
      fixed value, in a file that says its answers are not real.
    - "N/A": neither pattern was found; not a claim that the code is
      correct, only that this particular pattern check found nothing.

    This is a PATTERN match, not a control-flow proof - it does not know
    whether a "fail-closed" raise is reachable, or whether a trivial-body
    function is a legitimate constant (e.g. a feature-flag getter).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "N/A (sözdizimi hatası)"

    has_fail_closed = any(_raises_notimplemented_or_exception(f) for f in _function_nodes(tree))
    if has_fail_closed:
        return "Güvenli İskelet (Fail-Closed)"

    # The keyword side reads the SAME confession scan as ``real_or_mock``, not
    # a raw substring sweep: "there is deliberately NO hardcoded fallback"
    # next to any one-line accessor used to be enough to call a module risky.
    has_mock_keyword = bool(scan_real_or_mock_detailed(source).findings)
    has_trivial_function = any(_is_trivial_body(f) for f in _function_nodes(tree))
    if has_mock_keyword and has_trivial_function:
        return "Riskli Mock (Sessiz Hata)"

    return "N/A"


# --------------------------------------------------------------------------- #
# 7. Internal dependency radar (AST) - NEW in this port
# --------------------------------------------------------------------------- #
def find_internal_dependencies(source: str, project_module_stems: Set[str]) -> List[str]:
    """Walks ``Import``/``ImportFrom`` nodes and returns which OTHER
    modules IN THIS PROJECT the file imports (stdlib is ignored via
    ``sys.stdlib_module_names``; anything not in ``project_module_stems``
    - e.g. a genuinely third-party package - is ALSO excluded, since
    "internal" specifically means "another module found in this same
    scan", not merely "not stdlib").
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                found.add(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                continue  # a bare "from . import x" has no module name to record
            if node.module:
                found.add(node.module.split(".")[0])

    return sorted(name for name in found
                 if name not in stdlib and name in project_module_stems)


# --------------------------------------------------------------------------- #
# 8. External tool/network dependency scan (AST) - NEW in this port
# --------------------------------------------------------------------------- #
_SUBPROCESS_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
_REQUESTS_ATTRS = {"get", "post", "put", "delete", "patch", "request"}
# Recognisable external binaries this project actually shells out to -
# matched against the first positional argument's literal text so a
# generic "subprocess(...)" note becomes an actionable "subprocess(kicad-cli)".
_KNOWN_BINARIES = ("kicad-cli", "kicad_cli", "ngspice", "openems", "freerouting",
                   "pdftocairo", "pdftoppm", "git", "pytest")


def _call_root_name(call_node: ast.Call) -> Optional[str]:
    """"subprocess.run(...)" -> "subprocess"; "requests.get(...)" ->
    "requests"; anything else -> None."""
    func = call_node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _first_arg_literal_text(call_node: ast.Call) -> str:
    """Best-effort text of the call's first argument, for guessing which
    binary/URL a subprocess/request call targets. Handles a literal string,
    or a list/tuple whose first element is a literal string (the common
    ``subprocess.run(["kicad-cli", ...])`` shape) - anything else (a
    variable, an f-string) yields "" rather than a guess."""
    if not call_node.args:
        return ""
    first = call_node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return ""


def find_external_dependencies(source: str) -> str:
    """Walks ``Call`` nodes for ``subprocess.run``/``Popen``/etc. and
    ``requests.get``/``post``/etc. For a subprocess call, tries to name
    which binary is invoked from the first argument (falls back to
    "subprocess" if no known binary name is recognised in it). Returns
    "Yok" if nothing was found - never a guess presented as certain.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "N/A (sözdizimi hatası)"

    subprocess_targets: Set[str] = set()
    has_http = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        root = _call_root_name(node)

        if attr in _SUBPROCESS_ATTRS and (root == "subprocess" or attr in
                                          ("check_call", "check_output")):
            text = _first_arg_literal_text(node)
            matched = next((b for b in _KNOWN_BINARIES if b in text), None)
            subprocess_targets.add(matched or "subprocess")
        elif attr in _REQUESTS_ATTRS and root == "requests":
            has_http = True

    parts = [f"subprocess({name})" if name != "subprocess" else "subprocess"
            for name in sorted(subprocess_targets)]
    if has_http:
        parts.append("http_request")
    return ", ".join(parts) if parts else "Yok"


# --------------------------------------------------------------------------- #
# 9. AI contract detection (AST) - NEW in this port
# --------------------------------------------------------------------------- #
_AI_CONTRACT_BASE_NAMES = {"BaseModel", "TypedDict"}


def has_ai_contract(source: str) -> str:
    """Checks (via AST) whether the file defines a ``pydantic.BaseModel``
    subclass, a ``TypedDict``, or a ``@dataclass`` - i.e. whether an
    agent-facing structure in this file is STRUCTURED rather than a loose
    dict. Returns "Evet"/"Hayır" - presence only, not a schema validity
    check."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "Hayır"

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {b.id for b in node.bases if isinstance(b, ast.Name)}
        base_names |= {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
        if base_names & _AI_CONTRACT_BASE_NAMES:
            return "Evet"
        for decorator in node.decorator_list:
            # ``@dataclass(frozen=True)`` bir ``ast.Call``'dir, ``ast.Name``
            # DEGIL. Cagri bicimi taninmadigi icin dekoratorlu her dataclass
            # SESSIZCE "Hayir" sayiliyordu -- olculdu 2026-08-11: ``dru_emit.py``
            # ve ``fab_class.py`` zaten ``@dataclass(frozen=True)`` tasidiklari
            # halde envanterde sozlesmesiz gorunuyorlardi. Bir olcum aracinin
            # eksik saymasi, olctugu seyin yok sayilmasidir.
            if isinstance(decorator, ast.Call):
                decorator = decorator.func
            dec_name = decorator.id if isinstance(decorator, ast.Name) else (
                decorator.attr if isinstance(decorator, ast.Attribute) else None)
            if dec_name == "dataclass":
                return "Evet"
    return "Hayır"


# --------------------------------------------------------------------------- #
# 10. Last-change info (git log)
# --------------------------------------------------------------------------- #
def last_change_info(file: Path, repo_root: Path) -> Tuple[str, str]:
    """``(date, message)`` from ``git log -1``. Returns ("bilinmiyor", "")
    if there is no git history / it fails - never an invented date."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ad|%s", "--date=short", "--", str(file)],
            cwd=repo_root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "bilinmiyor", ""
    if result.returncode != 0 or not result.stdout.strip():
        return "bilinmiyor (commit edilmemiş olabilir)", ""
    date, _, message = result.stdout.strip().partition("|")
    return date, message


# --------------------------------------------------------------------------- #
# 11. Test status - a REAL pytest run (via junit-xml)
# --------------------------------------------------------------------------- #
def junit_test_module(classname: str) -> str:
    """The TEST FILE stem behind a junit ``classname``.

    pytest writes ``classname`` as the dotted import path and, for
    ``unittest.TestCase`` suites, APPENDS the class:
    ``tests.test_ids.DetUuidTests``. Taking ``split(".")[-1]`` therefore
    returned the CLASS name (``DetUuidTests``), which can never match a
    test FILE stem - so every lookup missed and ``test_status`` silently
    degraded to "olculemedi" for the whole repo, in the ONE column this
    module promises is really measured rather than estimated.

    Plain function-style tests carry no class part (``tests.test_ids``),
    so the LAST ``test_``-prefixed component is the correct key for both
    shapes. Falls back to the last component when nothing is prefixed
    that way, rather than inventing a key.
    """
    parts = [p for p in classname.split(".") if p]
    for part in reversed(parts):
        if part.startswith("test_"):
            return part
    return parts[-1] if parts else ""


def run_all_tests_and_collect_results(repo_root: Path, timeout_s: int = 900
                                      ) -> Dict[str, Tuple[int, int]]:
    """Runs the whole test suite ONCE with ``pytest --junit-xml`` (pytest
    also collects and runs plain ``unittest.TestCase`` suites, so this
    works for pcb-agent's unittest-based tests too), returns
    (passed, total) per test file (classname).

    A REAL run - a fixed/default "all passed" is NEVER invented. Returns an
    empty dict if pytest/xml could not be produced (e.g. an environment
    issue), which the caller must read as "could not be measured".
    """
    with tempfile.TemporaryDirectory() as tmp:
        xml_path = Path(tmp) / "results.xml"
        try:
            subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=no", f"--junit-xml={xml_path}"],
                cwd=repo_root, timeout=timeout_s, capture_output=True, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return {}
        if not xml_path.exists():
            return {}

        try:
            tree = ET.parse(xml_path)
        except ET.ParseError:
            return {}

        results: Dict[str, Tuple[int, int]] = {}
        for testcase in tree.getroot().iter("testcase"):
            classname = testcase.get("classname", "") or ""
            file_name = junit_test_module(classname) if classname else ""
            if not file_name:
                continue
            passed, total = results.get(file_name, (0, 0))
            total += 1
            failed = testcase.find("failure") is not None or testcase.find("error") is not None
            if not failed:
                passed += 1
            results[file_name] = (passed, total)
        return results


# --------------------------------------------------------------------------- #
# 12. One module's record + the full scan
# --------------------------------------------------------------------------- #
@dataclass
class ModuleRecord:
    module_name: str
    relative_path: str
    category: str
    status: str
    status_source: str
    summary: str
    has_test_file: bool
    test_function_count: int
    test_status: str  # "gecti" | "basarisiz" | "test_yok" | "olculemedi"
    real_or_mock: str
    mock_findings: List[str] = field(default_factory=list)
    # Screened-out hits with their reason. Kept OUT of the spreadsheet (it is a
    # decision table, not a log) and reported in the JSON run report instead,
    # so the screens can be audited without widening the CSV contract.
    mock_suppressed: List[str] = field(default_factory=list)
    last_change_date: str = "bilinmiyor"
    last_change_message: str = ""
    tool_chain_compat: str = "İlgisiz"
    security_status: str = "N/A"
    internal_dependencies: List[str] = field(default_factory=list)
    external_dependencies: str = "Yok"
    has_ai_contract: str = "Hayır"


_IMPORT_REFERENCE_CACHE: dict[Path, dict[str, Path]] = {}


def _test_reference_index(repo_root: Path) -> dict[str, Path]:
    """``module stem -> a test file that really imports it``.

    WHY THIS EXISTS: matching only ``test_<stem>.py`` counts a module as
    untested whenever the suite GROUPS its tests, which this repo does
    deliberately - ``test_board_checks.py`` covers four board modules,
    ``test_common.py`` eleven ``common/`` modules, ``test_production.py``
    four production modules. On a 97-module scan that convention alone
    produced 55 false "no test" rows against 3 genuinely untested modules:
    a 95 % false-positive rate, which makes the column worse than useless
    because a real gap cannot be seen in the noise.

    A REFERENCE MUST BE AN IMPORT OR AN EXPLICIT SCRIPT PATH, NOT A WORD
    MATCH. Searching for the bare stem anywhere in a test's text also
    matched comments and unrelated string literals - e.g.
    ``det_uuid("rewire", ...)`` passes the tool name as an argument without
    testing ``rewire.py`` at all. Two forms count here:

      * a real ``import`` / ``from ... import`` of the module, and
      * a string literal naming the script file (``"sync_tool_registry.py"``),
        which is how the CLI tools are exercised - ``test_tool_registry.py``
        runs ``SCRIPTS / "sync_tool_registry.py"`` as a subprocess and never
        imports it, yet genuinely tests it.
    """
    cached = _IMPORT_REFERENCE_CACHE.get(repo_root)
    if cached is not None:
        return cached

    index: dict[str, Path] = {}
    for test_file in repo_root.rglob("test_*.py"):
        if "tests" not in test_file.parts:
            continue
        try:
            source = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
                names += [f"{node.module}.{a.name}" for a in node.names]
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and node.value.endswith(".py")):
                # A script named as a path literal and run via subprocess.
                names = [Path(node.value).stem]
            for dotted in names:
                for part in dotted.split("."):
                    index.setdefault(part, test_file)

    _IMPORT_REFERENCE_CACHE[repo_root] = index
    return index


def _find_test_file(module_file: Path, repo_root: Path) -> Optional[Path]:
    """The test file covering ``module_file``, by name OR by real import.

    Resolution order, strongest evidence first:
      1. colocated ``test_<stem>.py`` (pcb-tool-v2's flat layout),
      2. ``tests/test_<stem>.py`` anywhere under the root (pcb-agent's
         layout),
      3. any test under ``tests/`` that genuinely IMPORTS the module -
         this is what makes grouped suites count (see
         ``_test_reference_index``).
    """
    # A package marker has no behaviour to test, and EVERY ``__init__.py``
    # shares the stem ``__init__`` - so a single test mentioning that name
    # would mark all twelve of them "tested". Excluded from the lookup
    # entirely; the Durum column already reports them as package markers.
    if module_file.name == "__init__.py":
        return None

    colocated = module_file.with_name(f"test_{module_file.stem}.py")
    if colocated.exists():
        return colocated
    target_name = f"test_{module_file.stem}.py"
    for candidate in repo_root.rglob(target_name):
        if "tests" in candidate.parts:
            return candidate
    return _test_reference_index(repo_root).get(module_file.stem)


def build_module_record(file: Path, repo_root: Path, project_module_stems: Set[str],
                        test_results: Optional[Dict[str, Tuple[int, int]]]) -> ModuleRecord:
    source = file.read_text(encoding="utf-8")

    category = determine_category(file.name, source)
    status, status_source = determine_status(source, file.relative_to(repo_root))
    summary = extract_summary(source)
    mock_scan = scan_real_or_mock_detailed(source, file.name)
    chain_compat = estimate_tool_chain_compat(source)
    date, message = last_change_info(file, repo_root)
    security_status = determine_security_status(source)
    internal_deps = find_internal_dependencies(source, project_module_stems)
    external_deps = find_external_dependencies(source)
    ai_contract = has_ai_contract(source)

    test_file = _find_test_file(file, repo_root)
    test_function_count = 0
    if test_file is not None:
        test_source = test_file.read_text(encoding="utf-8")
        test_function_count = len(re.findall(r"^\s*def test_\w+", test_source, re.M))

    if test_file is None:
        test_status = "test_yok"
    elif test_results is None:
        test_status = "olculemedi"
    else:
        key = test_file.stem
        if key not in test_results:
            test_status = "olculemedi"
        else:
            passed, total = test_results[key]
            test_status = "gecti" if (total > 0 and passed == total) else "basarisiz"

    return ModuleRecord(
        module_name=file.name, relative_path=str(file.relative_to(repo_root)),
        category=category, status=status, status_source=status_source, summary=summary,
        has_test_file=test_file is not None, test_function_count=test_function_count,
        test_status=test_status, real_or_mock=mock_scan.verdict,
        mock_findings=mock_scan.findings, mock_suppressed=mock_scan.suppressed,
        last_change_date=date, last_change_message=message, tool_chain_compat=chain_compat,
        security_status=security_status, internal_dependencies=internal_deps,
        external_dependencies=external_deps, has_ai_contract=ai_contract,
    )


def find_module_files(repo_root: Path) -> List[Path]:
    """Every ``.py`` file under ``repo_root`` EXCEPT ``test_*.py`` files and
    anything under an excluded directory (``.git``, ``.venv``,
    ``__pycache__``, ...). Recursive - unlike the source project's flat
    ``glob("*.py")``, this bundle's own tree is NOT flat
    (``common/``, ``scripts/board/``, ...).
    """
    files = []
    for path in repo_root.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def scan_repo(repo_root: Path, test_results: Optional[Dict[str, Tuple[int, int]]] = None
             ) -> List[ModuleRecord]:
    files = find_module_files(repo_root)
    project_module_stems = {f.stem for f in files}
    return [build_module_record(f, repo_root, project_module_stems, test_results) for f in files]


# --------------------------------------------------------------------------- #
# 13. Output writing (CSV always; XLSX if openpyxl is present)
# --------------------------------------------------------------------------- #
_STATUS_COLORS = {
    "Var": "C6EFCE",
    "Kısmen var": "FFEB9C",
    "İskelet": "D9D9D9",
    "KONTROL EDİLMELİ": "FFC7CE",
    "Riskli Mock (Sessiz Hata)": "FFC7CE",
    "Güvenli İskelet (Fail-Closed)": "C6EFCE",
}

_DETAIL_HEADERS = (
    "Modül", "Göreli Yol", "Kategori", "Durum", "Durum Kaynağı", "Özet",
    "Test Var mı", "Test Fonksiyon Sayısı", "Test Durumu",
    "Gerçek/Mock", "Mock Bulguları", "Son Değişiklik Tarihi",
    "Son Değişiklik Mesajı", "Araç Zinciri Uyumu",
    # --- new AST-based columns (this port) ---
    "Güvenlik Statüsü", "İç Bağımlılıklar", "Dış Bağımlılıklar", "AI Sözleşmesi Var Mı?",
)


def _record_row(r: ModuleRecord) -> list:
    return [
        r.module_name, r.relative_path, r.category, r.status, r.status_source, r.summary,
        "Evet" if r.has_test_file else "Hayır", r.test_function_count,
        r.test_status, r.real_or_mock, "; ".join(r.mock_findings),
        r.last_change_date, r.last_change_message, r.tool_chain_compat,
        r.security_status, ", ".join(r.internal_dependencies) or "Yok",
        r.external_dependencies, r.has_ai_contract,
    ]


def write_csv(records: List[ModuleRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_DETAIL_HEADERS)
        for r in records:
            writer.writerow(_record_row(r))


def write_excel(records: List[ModuleRecord], out_path: Path) -> bool:
    """Returns ``False`` (XLSX SKIPPED, CSV already written) if
    ``openpyxl`` is missing - never a hard crash."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill
    except ImportError:
        return False

    wb = openpyxl.Workbook()

    summary_ws = wb.active
    summary_ws.title = "Özet"
    summary_ws.append([f"YETENEK ENVANTERİ - son güncelleme: {datetime.now():%Y-%m-%d %H:%M}"])
    summary_ws.append([])
    summary_ws.append([
        "UYARI: 'Durum Kaynağı' sütunu 'otomatik_tahmin' olan satırlar KABA "
        "bir kestirimdir (fonksiyon sayısı/uzunluk), elle doğrulanmamıştır. "
        "Kritik bir karardan önce ilgili dosyayı ELLE kontrol edin."
    ])
    summary_ws.append([])
    summary_ws.append(["Kategori", "Var", "Kısmen var", "İskelet", "Toplam"])

    category_counts: Dict[str, Dict[str, int]] = {}
    for r in records:
        d = category_counts.setdefault(r.category, {"Var": 0, "Kısmen var": 0, "İskelet": 0})
        d[r.status] = d.get(r.status, 0) + 1

    for category in sorted(category_counts):
        d = category_counts[category]
        total = d["Var"] + d["Kısmen var"] + d["İskelet"]
        summary_ws.append([category, d["Var"], d["Kısmen var"], d["İskelet"], total])

    summary_ws.append([])
    total_modules = len(records)
    need_review = sum(1 for r in records if r.real_or_mock == "KONTROL EDİLMELİ")
    no_tests = sum(1 for r in records if r.test_status == "test_yok")
    risky_mock = sum(1 for r in records if r.security_status == "Riskli Mock (Sessiz Hata)")
    with_ai_contract = sum(1 for r in records if r.has_ai_contract == "Evet")
    summary_ws.append([f"Toplam modül: {total_modules}"])
    summary_ws.append([f"'KONTROL EDİLMELİ' işaretli modül: {need_review}"])
    summary_ws.append([f"Test dosyası OLMAYAN modül: {no_tests}"])
    summary_ws.append([f"'Riskli Mock (Sessiz Hata)' işaretli modül: {risky_mock}"])
    summary_ws.append([f"AI sözleşmesi (pydantic/TypedDict/dataclass) taşıyan modül: {with_ai_contract}"])

    detail_ws = wb.create_sheet("Detay")
    detail_ws.append(list(_DETAIL_HEADERS))
    for r in records:
        detail_ws.append(_record_row(r))

    status_col = _DETAIL_HEADERS.index("Durum") + 1
    mock_col = _DETAIL_HEADERS.index("Gerçek/Mock") + 1
    security_col = _DETAIL_HEADERS.index("Güvenlik Statüsü") + 1
    for row_no in range(2, len(records) + 2):
        for col in (status_col, mock_col, security_col):
            cell = detail_ws.cell(row=row_no, column=col)
            color = _STATUS_COLORS.get(cell.value)
            if color:
                cell.fill = PatternFill(start_color=color, end_color=color, fill_type="solid")

    detail_ws.auto_filter.ref = detail_ws.dimensions

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    clean_no_raise = "def f():\n    return 1\n"
    assert determine_security_status(clean_no_raise) == "N/A"

    fail_closed = "def f():\n    raise NotImplementedError('todo')\n"
    assert determine_security_status(fail_closed) == "Güvenli İskelet (Fail-Closed)"

    risky_mock_source = '"""this is a mock module"""\ndef f():\n    pass\n'
    assert determine_security_status(risky_mock_source) == "Riskli Mock (Sessiz Hata)"

    internal = find_internal_dependencies(
        "import os\nimport kicad_koprusu\nfrom hata_hafizasi import X\n",
        {"kicad_koprusu", "hata_hafizasi"})
    assert internal == ["hata_hafizasi", "kicad_koprusu"], internal

    external = find_external_dependencies(
        'import subprocess\nsubprocess.run(["ngspice", "-b", "x.cir"])\n')
    assert external == "subprocess(ngspice)", external
    assert find_external_dependencies("def f():\n    return 1\n") == "Yok"

    assert has_ai_contract(
        "from pydantic import BaseModel\nclass X(BaseModel):\n    y: int\n") == "Evet"
    assert has_ai_contract(
        "from dataclasses import dataclass\n@dataclass\nclass X:\n    y: int\n") == "Evet"
    assert has_ai_contract("class X:\n    pass\n") == "Hayır"

    diag("SELF-TEST PASS: security status, internal/external dependency radar, "
        "AI contract detection all verified")
    return 0


def measurement_status(records: List["ModuleRecord"],
                       test_results: Optional[Dict[str, Tuple[int, int]]]) -> Dict[str, object]:
    """Says out loud whether ``test_status`` was really measured.

    The junit key bug (see ``junit_test_module``) was invisible for exactly
    one reason: a suite that ran fine for 224 seconds and a suite whose
    results could not be MATCHED to any module both produced the same
    silent "olculemedi" in every row. A caller reading the spreadsheet had
    no way to tell "not measured on purpose" from "measurement plumbing is
    broken".

    So the run reports its own matching rate, and sets ``warning`` when a
    real pytest run produced results that matched NOTHING - the exact
    shape of that bug. Not an exit-code failure: the other columns are
    still valid, and turning a reporting tool red would hide them.
    """
    measured = sum(1 for r in records if r.test_status in ("gecti", "basarisiz"))
    with_test_file = sum(1 for r in records if r.has_test_file)
    junit_files = len(test_results) if test_results else 0
    warning = ""
    if test_results is None:
        state = "atlandi (--skip-tests)"
    elif junit_files == 0:
        state = "kosulamadi"
        warning = ("pytest calisti ama junit sonucu okunamadi - test_status "
                   "kolonu olculmedi")
    elif measured == 0:
        state = "eslesmedi"
        warning = (f"junit {junit_files} test dosyasi bildirdi ama HICBIRI bir modulle "
                   "eslesmedi - test_status kolonu guvenilmez")
    else:
        state = "olculdu"
    return {"state": state, "junit_test_files": junit_files,
            "modules_with_test_file": with_test_file,
            "modules_measured": measured, "warning": warning}


def mock_screen_status(records: List["ModuleRecord"]) -> Dict[str, object]:
    """What the confession screens threw away, and why.

    A narrowed filter has exactly one honest failure mode - narrowing too
    far - and it looks identical to a clean repo from the spreadsheet. So
    the suppressed hits ship with the run, grouped by reason, with the
    per-module detail kept for anyone re-checking a screen.
    """
    by_reason: Dict[str, int] = {}
    detail: Dict[str, List[str]] = {}
    for record in records:
        if record.mock_suppressed:
            detail[record.relative_path] = record.mock_suppressed
        for entry in record.mock_suppressed:
            reason = entry.split(" [")[0]
            by_reason[reason] = by_reason.get(reason, 0) + 1
    return {"flagged_modules": sum(1 for r in records if r.mock_findings),
            "suppressed_total": sum(len(r.mock_suppressed) for r in records),
            "suppressed_by_reason": by_reason,
            "suppressed_detail": detail}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_root", nargs="?")
    ap.add_argument("--out", default="YETENEK_ENVANTERI.xlsx")
    ap.add_argument("--skip-tests", action="store_true",
                    help="skip running the full suite (fast, but test_status stays 'olculemedi')")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.repo_root:
        ap.error("repo_root is required unless --self-test is given")

    repo_root = Path(a.repo_root).resolve()
    test_results = None if a.skip_tests else run_all_tests_and_collect_results(repo_root)
    records = scan_repo(repo_root, test_results)

    out_path = Path(a.out)
    if not out_path.is_absolute():
        out_path = repo_root / out_path
    csv_path = out_path.with_suffix(".csv")
    write_csv(records, csv_path)
    xlsx_written = write_excel(records, out_path)

    need_review = [r.module_name for r in records if r.real_or_mock == "KONTROL EDİLMELİ"]
    report = {
        "schema_version": SCHEMA_VERSION, "tool": TOOL_NAME, "repo_root": str(repo_root),
        "modules_scanned": len(records), "xlsx_written": xlsx_written,
        "xlsx_path": str(out_path) if xlsx_written else None, "csv_path": str(csv_path),
        "flagged_for_review": need_review,
        "test_measurement": measurement_status(records, test_results),
        "mock_screen": mock_screen_status(records),
    }
    if report["test_measurement"]["warning"]:
        diag(f"UYARI: {report['test_measurement']['warning']}")
    write_report(report, a.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
