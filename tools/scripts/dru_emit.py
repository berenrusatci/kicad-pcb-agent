#!/usr/bin/env python3
"""Emit a KiCad 10 ``.kicad_dru`` custom-rules file from the IPC calculators.

This is the bridge that turns the numbers produced by ``scripts/ipc/*`` into
rules KiCad will actually ENFORCE, closing the loop from calculation to DRC.

THREE SILENT TRAPS, EMPIRICALLY CONFIRMED AGAINST REAL KiCad 10
----------------------------------------------------------------
A previous revision of this project wrote a ``.kicad_dru`` file, ran
``kicad-cli pcb drc`` against it, and discovered the rules were SILENTLY
never applied: kicad-cli emitted no error and no warning, it simply ignored
the file, and the DRC run LOOKED like the rules were in force. The detection
method was to add a global ``(constraint clearance (min 3mm))`` probe and
check whether the violation count actually exploded (it does if the file is
being read; it stays at baseline if it is not).

This module makes all three traps STRUCTURALLY impossible:

  1. ``(version 1)`` is MANDATORY. Without it the ENTIRE file is silently
     ignored. ``render_rules`` always writes it as the first line, so a
     caller cannot forget it.
  2. ``(priority N)`` IS NOT A VALID TOKEN. Adding it to a rule silently
     disables that rule. This module offers no priority field at all —
     precedence in KiCad is by FILE ORDER (first matching rule wins), and
     ``RuleOrder`` encodes that ordering explicitly: component-specific
     EXCEPTIONS are always written BEFORE general rules.
  3. THE COMMENT CHARACTER IS ``#``, NOT ``;``. A ``;`` breaks the parse and
     the WHOLE file is discarded. Every comment this module emits starts
     with ``#``; ``;`` is rejected in rule names and conditions.

VERIFICATION STATUS — read before trusting a constraint type
-------------------------------------------------------------
Against a real board (ESP32C3_SmartBand.kicad_pcb, KiCad 10.0.4), with the
original ``.kicad_dru`` safely backed up and byte-identically restored
afterwards:

  * ``clearance`` probe (global, 3 mm min): 3 baseline violations -> 503.
    Proof the file was really being read.
  * ``annular_width`` probe (vias, deliberately absurd 5 mm min): produced
    187 ``annular_width`` violations, each correctly reporting the rule name
    and the exact limit.
  * ``track_width``: likewise confirmed.

NOT verified in that run, and flagged in each emitted rule's comment:
  * two-net-class conditions (``A.NetClass == 'X' && B.NetClass == 'Y'``)
  * ``courtyard_clearance`` as a constraint type

Those must be re-confirmed on your machine with the same probe technique
(deliberately absurd limit, check the violation count really explodes)
before they are trusted. Until then keep checking those risks with the
Python-side tools (``scripts/ipc/dfa.py``, ``dfm_emc_check.py``).

WHAT CANNOT BE EXPRESSED AS A DRU RULE
---------------------------------------
KiCad Custom Rules has no native constraint for solder-mask dam width or
hole aspect ratio. Those two IPC-6012 limits CANNOT be written here and must
continue to be checked in Python against real Gerber / board data by
``scripts/ipc/fab_class.py``.

CLI
---
    dru_emit.py RULES_JSON --out board.kicad_dru [--json OUT] [--self-test]

    exit 0  rules rendered (and written, when --out was given)
    exit 2  bad or missing input, or self-test failure
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from scripts.ipc import clearance as clearance_mod  # noqa: E402
from scripts.ipc import dfa as dfa_mod  # noqa: E402
from scripts.ipc import fab_class as fab_class_mod  # noqa: E402
from scripts.ipc import trace_width as trace_width_mod  # noqa: E402

SCHEMA_VERSION = "dru-emit-report/v1"
TOOL_NAME = "dru_emit"

# Constraint types this project has PROVEN are enforced by real kicad-cli.
VERIFIED_CONSTRAINTS = frozenset({"track_width", "clearance", "annular_width"})

UNVERIFIED_NOTE = (
    "NOT VERIFIED in this project against real kicad-cli pcb drc — confirm "
    "with a deliberately absurd probe limit before trusting it")


# --------------------------------------------------------------------------- #
# AI SÖZLEŞMESİ — ajanın aracı çağırabilmesi için girdi/çıktı yapısı. Girdi
# `_build_parser()` argparse bloğundan, çıktı `main()` içinde ELLE kurulan
# `report` sözlüğünden türetildi (bu araç ortak bir zarf yardımcısı kullanmaz,
# o yüzden kaynak doğrudan o sözlüktür).
# --------------------------------------------------------------------------- #
class DruEmitInputOptional(TypedDict, total=False):
    """Zorunlu OLMAYAN argümanlar. Kaynak: `_build_parser()` (s.539-552).

    `backup` DÜZ MANTIKTIR ama bayrak TERSTİR: CLI'da `--no-backup` vardır ve
    `dest="backup"` + `store_false` ile bu alan varsayılan True olur. Yani
    "yedek al" varsayılandır; ajanın açıkça False vermesi gerekir.
    """

    out: str            # --out: .kicad_dru dosyasının yazılacağı yol (verilmezse yalnız rapor)
    backup: bool        # --no-backup ile False olur; VARSAYILAN True (varsa .bak kopyası alınır)
    json: str           # --json: JSON raporunun da yazılacağı yol
    self_test: bool     # --self-test: üç sessiz tuzağı ve kusur enjeksiyonunu koşar, çıkar


class DruEmitInput(DruEmitInputOptional):
    """Aracın tam girdisi. Kaynak: `_build_parser()` (s.539-552).

    `rules` argparse'ta `nargs="?"` ile tanımlıdır, ama `--self-test`
    verilmediyse `parser.error()` ile ZORUNLU kılınır (s.568-570): pratik kural
    `rules` ya da `self_test` — biri şart. `rules` dosyasının içeriği
    `rules_from_spec()`'in kabul ettiği bölümlerdir (`track_width`, `clearance`,
    `annular_ring`, `exceptions`, `name_prefix`, `header`); hepsi isteğe
    bağlıdır ama HİÇ kural üretmeyen bir spec REDDEDİLİR — boş bir .kicad_dru
    "kural istenmedi" ile "kurallar unutuldu" ayrımını silerdi.
    """

    rules: str          # konumsal: JSON kural spec'i


class DruEmitOutput(TypedDict):
    """`--json` ile yazılan raporun üst düzey anahtarları. Kaynak: `main()`
    içindeki `report` sözlüğü (bu dosyada s.591-621). Bu araca ait bir
    `schemas/*.schema.json` YOKTUR, o yüzden tek kaynak kodun kendisidir.

    İki alan sessiz kabul yerine AÇIK UYARI taşır ve bu yüzden sözleşmenin
    parçasıdır: `unverified_constraint_types` ve `unverified_two_net_class_rules`
    boş DEĞİLSE, üretilen kuralların gerçek kicad-cli tarafından uygulandığı bu
    projede KANITLANMAMIŞTIR (bkz. dosya başı "VERIFICATION STATUS").

    ÇIKIŞ KODLARI (dosya başı notu): 0 = kurallar üretildi (ve `--out` verildiyse
    yazıldı), 2 = bozuk/eksik girdi ya da self-test başarısız. Bu araç 1
    DÖNDÜRMEZ: kural üretmek bir denetim değildir, "ihlal" kavramı yoktur.
    """

    schema_version: str                     # "dru-emit-report/v1"
    tool: str                               # "dru_emit"
    spec: str                               # okunan kural spec'inin yolu
    written_to: str | None                  # --out verilmediyse None (yazılmadı, gizlenmez)
    rule_count: int                         # üretilen kural sayısı (0 olamaz; boş spec reddedilir)
    rules: list                             # {name, condition, constraint_type, min_value_mm, order, verified_constraint}
    unverified_constraint_types: list       # VERIFIED_CONSTRAINTS dışında kalan kısıt türleri
    unverified_two_net_class_rules: list    # koşulunda '&&' geçen kuralların adları
    dru_text: str                           # üretilen .kicad_dru metninin tamamı
    limits: list                            # aracın KENDİ sınırları (mask dam / aspect ratio DRU'da ifade EDİLEMEZ)


class RuleOrder(int, Enum):
    """KiCad has NO real ``priority`` field (see the module docstring).

    This enum exists only INSIDE this module, to sort rules into file order.
    It is never written into the generated ``.kicad_dru`` text.
    """

    EXCEPTION = 0   # component-specific exceptions — written FIRST, so they win
    GENERAL = 1     # net-class-wide rules — written after


@dataclass(frozen=True)
class DruRule:
    name: str
    condition: str
    constraint_type: str        # track_width | clearance | annular_width | ...
    min_value_mm: float
    order: RuleOrder = RuleOrder.GENERAL
    comment: str = ""

    def __post_init__(self) -> None:
        if self.min_value_mm <= 0:
            raise ValueError(
                f"{self.name}: min_value_mm must be positive, "
                f"got {self.min_value_mm!r}")
        if "\n" in self.name or "\n" in self.condition:
            raise ValueError(
                f"{self.name}: no newlines allowed in name/condition — they are "
                "embedded inside an s-expression string field and a newline "
                "breaks the s-expression.")
        # The ';' ban applies to name/condition ONLY. Those are short
        # identifiers embedded INSIDE the (rule "...")/(condition "...")
        # s-expression structure, so banning the character outright is the
        # safest choice. `comment` is different: render() always wraps it in
        # its own '# ' prefixed line, and a ';' INSIDE a '#' line is
        # completely harmless in KiCad's DRU language (the whole line is
        # already a comment). The real risk for `comment` is a NEWLINE — a
        # second line would lose the '#' prefix and be parsed as an
        # s-expression token — which is why render() prefixes EVERY line.
        if ";" in self.name or ";" in self.condition:
            raise ValueError(
                f"{self.name}: ';' is forbidden in name/condition — KiCad's "
                "comment character is '#', and ';' SILENTLY breaks the parse "
                "of the entire file (see the module docstring).")

    def render(self) -> str:
        lines = [f"# {line}" for line in self.comment.splitlines()] \
            if self.comment else []
        comment = ("\n".join(lines) + "\n") if lines else ""
        return (
            f"{comment}"
            f'(rule "{self.name}"\n'
            f'  (condition "{self.condition}")\n'
            f"  (constraint {self.constraint_type} "
            f"(min {self.min_value_mm}mm)))")


def render_rules(rules: Sequence[DruRule], header: str = "") -> str:
    """Render the complete ``.kicad_dru`` text.

    Ordering: every ``RuleOrder.EXCEPTION`` rule is written BEFORE every
    ``RuleOrder.GENERAL`` rule — the direct consequence of trap #2, "first
    matching rule wins". Python's ``sorted`` is stable, so relative order
    within a group is preserved.
    """
    ordered = sorted(rules, key=lambda rule: rule.order.value)
    body: list[str] = ["(version 1)", ""]
    if header:
        for line in header.splitlines():
            body.append(f"# {line}" if line.strip() else "#")
        body.append("")
    for index, rule in enumerate(ordered):
        body.append(rule.render())
        if index != len(ordered) - 1:
            body.append("")
    return "\n".join(body) + "\n"


def write_rules_file(target: str | Path, rules: Sequence[DruRule],
                     header: str = "", backup: bool = True) -> str:
    """Write the rules to a real ``.kicad_dru`` file.

    Same "back up first, then overwrite" pattern the rest of the bundle
    uses. One ``.kicad_dru`` per project is assumed; the file is OVERWRITTEN,
    never appended to.

    An empty rule list raises. Writing an empty ``.kicad_dru`` would hide the
    difference between "there are deliberately no custom rules" and "the
    rules were forgotten"; a caller who really wants an empty file must write
    it themselves rather than have this function do it silently.
    """
    if not rules:
        raise ValueError(
            "rule list is empty — if you deliberately want an empty "
            ".kicad_dru, do NOT call this function; write the file directly.")

    path = Path(target)
    if backup and path.exists():
        shutil.copy(path, path.with_suffix(path.suffix + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_rules(rules, header), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------- #
# IPC result -> DruRule converters
# --------------------------------------------------------------------------- #
def rule_from_trace_width(net_class: str,
                          result: trace_width_mod.TraceWidthResult,
                          prefix: str = "") -> DruRule:
    """A minimum track width rule for one net class. VERIFIED constraint."""
    return DruRule(
        name=f"{prefix}IPC-2152 {net_class} Min Width ({result.layer_type})",
        condition=f"A.NetClass == '{net_class}'",
        constraint_type="track_width",
        min_value_mm=result.width_mm,
        order=RuleOrder.GENERAL,
        comment=(f"IPC-2152 (scripts/ipc/trace_width.py): {result.current_a} A, "
                 f"dT={result.delta_t_c} C, {result.copper_oz} oz, "
                 f"inner-derating={result.inner_derating}"),
    )


def rule_from_clearance(net_class_a: str, net_class_b: str,
                        result: clearance_mod.ClearanceResult,
                        prefix: str = "") -> DruRule:
    """A minimum clearance rule between two net classes.

    The two-net-class condition form is NOT verified in this project against
    real kicad-cli — unlike the single-piece clearance probe that was. The
    caveat is written into the rule's own comment so it survives into the
    generated file.
    """
    return DruRule(
        name=f"{prefix}IPC-2221 {net_class_a}-{net_class_b} Clearance",
        condition=f"A.NetClass == '{net_class_a}' && B.NetClass == '{net_class_b}'",
        constraint_type="clearance",
        min_value_mm=result.clearance_mm,
        order=RuleOrder.GENERAL,
        comment=(f"IPC-2221B (scripts/ipc/clearance.py): {result.voltage_v} V, "
                 f"{result.layer_type}/{result.coating_state}, "
                 f"confidence={result.confidence}. "
                 f"two-net-class condition {UNVERIFIED_NOTE}"),
    )


def rules_from_fab_class(limits: fab_class_mod.ClassLimits,
                         prefix: str = "") -> list[DruRule]:
    """The annular-ring rule for all vias. VERIFIED constraint.

    Only ``annular_width`` can be expressed. IPC-6012's solder-mask dam and
    aspect-ratio limits have NO native KiCad constraint and must keep being
    checked by scripts/ipc/fab_class.py against real board/Gerber data.
    """
    return [
        DruRule(
            name=f"{prefix}IPC-6012 {limits.fab_class.value} Min Annular Ring",
            condition="A.Type == 'Via'",
            constraint_type="annular_width",
            min_value_mm=limits.min_annular_ring_mm,
            order=RuleOrder.GENERAL,
            comment=(f"IPC-6012 {limits.fab_class.value} "
                     f"(scripts/ipc/fab_class.py): {limits.source_note}"),
        )
    ]


def rule_from_three_w(net_class_a: str, net_class_b: str, result: Any,
                      prefix: str = "") -> DruRule:
    """A 3W crosstalk clearance rule between two net classes.

    KiCad's ``clearance`` constraint measures COPPER EDGE TO EDGE, not
    centre to centre, so the edge-to-edge figure is used. Confusing those
    two is the single most common 3W mistake in the industry.

    Duck-typed on purpose: the caller passes any object exposing
    ``min_edge_to_edge_mm`` (as ``scripts.emc_rules.ThreeWTarget`` does), so
    this module does not hard-depend on the EMC engine's internal class. The
    longer ``minimum_edge_to_edge_mm`` spelling is also accepted.
    """
    edge_to_edge = getattr(result, "min_edge_to_edge_mm", None)
    if edge_to_edge is None:
        edge_to_edge = getattr(result, "minimum_edge_to_edge_mm", None)
    if edge_to_edge is None:
        raise ValueError(
            "3W result must expose min_edge_to_edge_mm; KiCad clearance is "
            "edge-to-edge and a centre-to-centre value would silently "
            "under-constrain the rule.")
    centre = getattr(result, "min_center_to_center_mm",
                     getattr(result, "minimum_centre_to_centre_mm", "?"))
    return DruRule(
        name=f"{prefix}EMI 3W {net_class_a}-{net_class_b} Crosstalk Clearance",
        condition=f"A.NetClass == '{net_class_a}' && B.NetClass == '{net_class_b}'",
        constraint_type="clearance",
        min_value_mm=edge_to_edge,
        order=RuleOrder.GENERAL,
        comment=(f"3W rule (scripts/emc_rules.py): centre-to-centre target "
                 f"{centre} mm, written here as edge-to-edge {edge_to_edge} mm. "
                 f"two-net-class condition {UNVERIFIED_NOTE}"),
    )


def rule_from_dfa(ref_a: str, ref_b: str,
                  target: dfa_mod.ClearanceTarget,
                  prefix: str = "") -> DruRule:
    """A component-to-component courtyard clearance rule.

    Marked as an EXCEPTION so it is written before the net-class rules.

    BE MORE CAUTIOUS WITH THIS ONE than with the others: unlike the proven
    track_width / clearance / annular_width trio, ``courtyard_clearance`` has
    NEVER been tested in this project against real kicad-cli. It appears in
    the documentation, but the three traps above were only proven for the
    verified trio. Confirm it with the absurd-probe technique before relying
    on it; otherwise keep checking this with scripts/ipc/dfa.py.
    """
    comment = (f"IPC-A-610 DFA (scripts/ipc/dfa.py): {target.type_a}-"
               f"{target.type_b}, {target.assembly_class}")
    if target.shadowing_risk:
        comment += ", includes the reflow shadowing surcharge"
    comment += f". courtyard_clearance {UNVERIFIED_NOTE}"
    return DruRule(
        name=f"{prefix}DFA {ref_a}-{ref_b} Courtyard Clearance",
        condition=f"A.Reference == '{ref_a}' && B.Reference == '{ref_b}'",
        constraint_type="courtyard_clearance",
        min_value_mm=target.minimum_clearance_mm,
        order=RuleOrder.EXCEPTION,
        comment=comment,
    )


def exception_rule(name: str, reference: str, min_value_mm: float,
                   constraint_type: str = "clearance",
                   comment: str = "") -> DruRule:
    """A single component's internal pad-to-pad exception
    (``A.Reference == B.Reference == ref``). Marked EXCEPTION so it is
    written before the general rules."""
    return DruRule(
        name=name,
        condition=f"A.Reference == '{reference}' && B.Reference == '{reference}'",
        constraint_type=constraint_type,
        min_value_mm=min_value_mm,
        order=RuleOrder.EXCEPTION,
        comment=comment,
    )


# --------------------------------------------------------------------------- #
# Spec -> rules
# --------------------------------------------------------------------------- #
def rules_from_spec(spec: dict) -> list[DruRule]:
    """Build the rule list from the input document.

    See the module docstring for the accepted shape. Every section is
    optional, but an empty result is rejected by the caller so a spec that
    produced nothing cannot quietly become an empty rules file.
    """
    if not isinstance(spec, dict):
        raise ValueError("rules file must contain a JSON object")
    prefix = str(spec.get("name_prefix", ""))
    rules: list[DruRule] = []

    for entry in spec.get("track_width", []):
        layer = trace_width_mod.LayerType(entry.get("layer", "external"))
        result = trace_width_mod.min_trace_width(
            float(entry["current_a"]),
            float(entry.get("delta_t_c", 10.0)),
            layer,
            float(entry.get("copper_oz", 1.0)),
            float(entry.get("inner_derating",
                            trace_width_mod.DEFAULT_INNER_DERATING)),
        )
        rules.append(rule_from_trace_width(str(entry["net_class"]), result, prefix))

    for entry in spec.get("clearance", []):
        result = clearance_mod.clearance_mm(
            float(entry["voltage_v"]),
            clearance_mod.LayerType(entry.get("layer", "external")),
            clearance_mod.CoatingState(entry.get("coating", "uncoated")),
            bool(entry.get("interpolate", False)),
            float(entry.get("creepage_factor",
                            clearance_mod.DEFAULT_CREEPAGE_FACTOR)),
        )
        rules.append(rule_from_clearance(str(entry["net_class_a"]),
                                         str(entry["net_class_b"]),
                                         result, prefix))

    annular = spec.get("annular_ring")
    if annular:
        fab_class = fab_class_mod.FabClass(annular.get("class", "Class 2"))
        rules.extend(rules_from_fab_class(
            fab_class_mod.CLASS_LIMITS[fab_class], prefix))

    for entry in spec.get("exceptions", []):
        rules.append(exception_rule(
            str(entry["name"]), str(entry["ref"]), float(entry["min_mm"]),
            str(entry.get("constraint", "clearance")),
            str(entry.get("comment", "")),
        ))

    return rules


# --------------------------------------------------------------------------- #
# Self-test + fault injection
# --------------------------------------------------------------------------- #
def _prove_test_not_empty() -> bool:
    """FAULT INJECTION: a rule name containing ';' must raise. If it were
    accepted it would silently become an invalid line in the generated file
    and take the whole ruleset down with it."""
    try:
        DruRule("Bad;Name", "A.NetClass == 'X'", "clearance", 0.2)
    except ValueError:
        return True
    return False


def run_self_tests() -> list[str]:
    failures: list[str] = []

    rule = DruRule("Test", "A.NetClass == 'X'", "clearance", 0.2)
    text = render_rules([rule])

    # Trap #1
    if not text.startswith("(version 1)"):
        failures.append("generated file does not start with '(version 1)'")
    # Trap #2
    if "(priority" in text:
        failures.append("generated file contains the invalid '(priority' token")
    # Trap #3
    commented = DruRule("Test2", "A.NetClass == 'Y'", "clearance", 0.3,
                        comment="note")
    if ";" in render_rules([commented]):
        failures.append(
            "generated file contains ';' (the comment character must be '#')")

    # A multi-line comment must prefix EVERY line, or line 2 becomes a stray
    # s-expression token and the whole file dies.
    multiline = DruRule("Test3", "A.NetClass == 'Z'", "clearance", 0.3,
                        comment="line one\nline two")
    for line in render_rules([multiline]).splitlines():
        if line.strip() in ("line one", "line two"):
            failures.append("a comment line lost its '#' prefix")

    # Exceptions must be written before general rules, even when supplied
    # in the opposite order.
    general = DruRule("General", "A.NetClass == 'PWR'", "track_width", 0.25,
                      order=RuleOrder.GENERAL)
    exception = exception_rule("J2 Exception", "J2", 0.13)
    ordered_text = render_rules([general, exception])   # deliberately reversed
    if ordered_text.index('"J2 Exception"') > ordered_text.index('"General"'):
        failures.append(
            "the exception rule was written AFTER the general rule "
            "(file-order precedence is broken)")

    # An empty rule list must be refused.
    try:
        write_rules_file("unused.kicad_dru", [])
    except ValueError:
        pass
    else:
        failures.append("writing a file with an empty rule list was not refused")

    # A non-positive minimum must be refused.
    try:
        DruRule("Zero", "A.NetClass == 'X'", "clearance", 0.0)
    except ValueError:
        pass
    else:
        failures.append("a non-positive min_value_mm was not refused")

    if not _prove_test_not_empty():
        failures.append(
            "fault injection did not fire: the ';' validation may be empty")

    return failures


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rules", type=Path, nargs="?",
                        help="JSON rule spec (required unless --self-test)")
    parser.add_argument("--out", type=Path,
                        help="write the .kicad_dru file here")
    parser.add_argument("--no-backup", dest="backup", action="store_false",
                        help="do not copy an existing .kicad_dru to .bak first")
    parser.add_argument("--json", type=Path,
                        help="also write the JSON report to this path")
    parser.add_argument("--self-test", dest="self_test", action="store_true",
                        help="run self-tests and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.self_test:
        failures = run_self_tests()
        for failure in failures:
            diag(f"SELF-TEST FAIL: {failure}")
        if failures:
            return 2
        diag("SELF-TEST PASS")
        return 0

    if args.rules is None:
        parser.error("a rules JSON file is required; pass it positionally "
                     "or use --self-test")

    spec = json.loads(Path(args.rules).read_text(encoding="utf-8"))
    rules = rules_from_spec(spec)
    if not rules:
        parser.error(
            "the spec produced no rules — refusing to emit an empty "
            ".kicad_dru, which would hide 'no rules wanted' behind "
            "'rules forgotten'")

    header = str(spec.get("header", ""))
    text = render_rules(rules, header)

    written = None
    if args.out is not None:
        written = write_rules_file(args.out, rules, header, backup=args.backup)

    unverified = sorted({rule.constraint_type for rule in rules
                         if rule.constraint_type not in VERIFIED_CONSTRAINTS})
    two_class = sorted({rule.name for rule in rules if "&&" in rule.condition})

    report = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "spec": str(args.rules),
        "written_to": written,
        "rule_count": len(rules),
        "rules": [
            {
                "name": rule.name,
                "condition": rule.condition,
                "constraint_type": rule.constraint_type,
                "min_value_mm": rule.min_value_mm,
                "order": rule.order.name,
                "verified_constraint":
                    rule.constraint_type in VERIFIED_CONSTRAINTS,
            }
            for rule in sorted(rules, key=lambda r: r.order.value)
        ],
        "unverified_constraint_types": unverified,
        "unverified_two_net_class_rules": two_class,
        "dru_text": text,
        "limits": [
            "Solder-mask dam width and hole aspect ratio have NO native "
            "KiCad constraint and CANNOT be emitted here; keep checking them "
            "with scripts/ipc/fab_class.py.",
            "Only track_width, clearance and annular_width have been proven "
            "to be enforced by real kicad-cli in this project.",
            "Two-net-class conditions and courtyard_clearance are UNVERIFIED "
            "here; confirm with an absurd probe limit before trusting them.",
        ],
    }
    if unverified:
        diag(f"WARNING: unverified constraint types emitted: {unverified}")
    if two_class:
        diag(f"WARNING: {len(two_class)} two-net-class rule(s) emitted; "
             "this condition form is unverified in this project")

    write_report(report, args.json)
    emit(report)
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
