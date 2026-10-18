#!/usr/bin/env python3
"""CAD asset gate: a downloaded symbol/footprint is not trusted until its
pin count is proven against the datasheet.

backend: cli  (network: NONE - see below)

WHY THIS TOOL EXISTS
-------------------------------------------------------------------------
``jlc_library`` covers the LCSC/JLCPCB catalogue. A part that is stocked at
DigiKey/Mouser but absent from LCSC had no path at all: draw it by hand, or
do not use the part. The archive's ``cad_api_koprusu.py`` closed that gap
and was rejected during the port as "network dependent, never run".

Measured, that rejection was wrong for the same reason
``mekanik_dxf_koprusu``'s was: the download is the small part. The VALUE is
the two gates around it, and both are pure offline computation this bundle
did not otherwise have anywhere:

  1. lifecycle BEFORE download - downloading the footprint of an NRND part
     and dropping it into a schematic is the quietest way to lock a dead
     part into a design;
  2. pin count BEFORE the asset is registered - internet symbols really do
     ship with missing pins (exposed pads especially), and a symbol from
     one variant paired with a footprint from another passes ERC and DRC
     and is only discovered when the first board is soldered.

WHAT THIS TOOL DOES NOT DO: FETCH
-------------------------------------------------------------------------
No HTTP call is made here. ``download_allowed()`` answers whether a URL is
acceptable (HTTPS + a deliberately narrow host allowlist) and the agent
performs the transfer, exactly as ``bom_lifecycle`` splits the supply
query. Two reasons, not one:

  * a tool that fetches cannot be run deterministically in a test, which is
    how the archive module ended up "never run";
  * pulling an arbitrary URL into a project as a KiCad library - which
    KiCad then parses - is a supply-chain step, and it belongs somewhere a
    human is looking.

Usage:
    cad_assets.py <symbol.kicad_sym> --datasheet-pins 48 [--footprint x.kicad_mod]
                  [--json out.json]
    cad_assets.py --register <symbol.kicad_sym> --project DIR --lib-name NICK
                  --datasheet-pins 48 [--footprint x.kicad_mod]
    cad_assets.py --self-test

Exit: 0 PASS, 1 FAIL/NO_COVERAGE, 2 crash (run_cli).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.findings import result, summarise  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.sexpr import parse_sexpr, unquote  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bom_lifecycle import BLOCKING_LIFECYCLES, LIFECYCLE_VALUES  # noqa: E402

SCHEMA_VERSION = "cad-assets-report/v1"
TOOL_NAME = "cad_assets"

# Downloads come from these hosts or not at all. A narrow list is the point:
# a library file is parsed by KiCad on open, so an arbitrary URL is code
# reaching a parser.
ALLOWED_HOSTS: Tuple[str, ...] = (
    "snapeda.com", "www.snapeda.com", "api.snapeda.com",
    "api.nexar.com", "app.ultralibrarian.com",
)

_LIB_LINE = '  (lib (name "{nick}")(type "KiCad")(uri "{uri}")(options "")(descr "{descr}"))\n'


# --------------------------------------------------------------------------- #
# 1. Gate one - lifecycle, before anything is downloaded
# --------------------------------------------------------------------------- #
def lifecycle_gate(mpn: str, lifecycle: Optional[str]) -> Tuple[bool, str]:
    """``(allowed, reason)`` for MASTER_RULEBOOK Bölüm 0's BOM rule.

    NRND/EOL/Obsolete is never allowed - there is no "good enough for a
    prototype" exception, because the prototype's library entry is what the
    production design inherits.

    An UNKNOWN lifecycle does not block, but the reason says CONFIRM out
    loud. Treating "we did not check" as "Active" is the failure this gate
    exists to prevent, so it is never silently upgraded.
    """
    if lifecycle in BLOCKING_LIFECYCLES:
        return False, (f"{mpn}: lifecycle={lifecycle} - MASTER_RULEBOOK Bölüm 0 'BOM Kuralı "
                       "(Kalıcı, İstisnasız)': CAD varlığı indirilmez, Active alternatife geç.")
    if lifecycle not in LIFECYCLE_VALUES or lifecycle == "Unknown":
        return True, (f"{mpn}: lifecycle BİLİNMİYOR -> CONFIRM. İndirmeye izin verildi ama "
                      "üretim durumu doğrulanmadan şematiğe işlenmemeli.")
    return True, f"{mpn}: lifecycle={lifecycle} - indirmeye izin verildi."


def download_allowed(url: str) -> Tuple[bool, str]:
    """HTTPS and a host on the allowlist, or refuse with the reason."""
    parts = urlparse(url)
    if parts.scheme != "https":
        return False, f"{url}: yalnız https kabul edilir (şema={parts.scheme or 'yok'})"
    if parts.netloc.lower() not in ALLOWED_HOSTS:
        return False, f"{url}: host beyaz listede değil ({parts.netloc or 'yok'})"
    return True, f"{url}: izinli"


# --------------------------------------------------------------------------- #
# 2. Gate two - pin count
# --------------------------------------------------------------------------- #
def _numbers_under(tree, parent: str, child: str = "number") -> List[str]:
    """Every ``(child "N")`` that sits DIRECTLY under a ``(parent ...)``.

    Read structurally rather than with ``\\(number\\s+"..."`` over the raw
    text (which is what the archive did): a regex cannot tell a pin's number
    from the same token appearing anywhere else in the file, and a KiCad
    format that moves the token breaks it silently. The nesting is the
    thing being asserted, so the nesting is what gets read.
    """
    found: List[str] = []

    def walk(node):
        if not isinstance(node, list):
            return
        if node and node[0] == parent:
            for item in node:
                if isinstance(item, list) and len(item) > 1 and item[0] == child:
                    found.append(unquote(item[1]))
        for item in node:
            walk(item)

    walk(tree)
    return found


def symbol_pin_numbers(symbol_text: str) -> List[str]:
    """UNIQUE pin numbers in a ``.kicad_sym``.

    Unique because a multi-unit symbol, or one with a De Morgan alternate
    body style, defines the same pin more than once. Counting raw ``(pin``
    forms inflates the total two- or threefold and turns the datasheet
    comparison into a false FAIL.
    """
    return sorted(set(_numbers_under(parse_sexpr(symbol_text), "pin")))


def footprint_pad_numbers(footprint_text: str) -> List[str]:
    """UNIQUE pad numbers in a ``.kicad_mod``.

    The exposed/thermal pad is NOT filtered out. It arrives as its own
    number (``"EP"``, ``"41"``, ...) and MASTER_RULEBOOK Faz 1 requires it
    to be counted and tied to GND - dropping it here would hide precisely
    the mismatch this check is for.

    A NUMBERLESS pad IS filtered out, and that is a different decision made
    for the opposite reason. KiCad writes a mounting hole as
    ``(pad "" np_thru_hole circle ...)``: the empty string is not a pad
    number, it is the absence of one. Keeping it put ``""`` into the set, so
    a CORRECT footprint carrying one mounting hole reported
    ``footprint_vs_sembol: 6 pad / 5 pin; sembolde olmayan pad=['']`` - a
    gate failing on a board that is right. Measured 2026-08-10 while
    building cad_asset_qualification, which detects the same artifact and
    excludes it with a recorded reason.

    The asymmetry is the point: the exposed pad is a pin the symbol must
    also carry, and a mounting hole is not connected to anything. Counting
    the first is what the gate is for; counting the second is a false
    positive, and a gate that cries wolf is switched off by the third time.
    """
    pads: List[str] = []

    def walk(node):
        if not isinstance(node, list):
            return
        if len(node) > 1 and node[0] == "pad" and isinstance(node[1], str):
            number = unquote(node[1])
            if number:
                pads.append(number)
        for item in node:
            walk(item)

    walk(parse_sexpr(footprint_text))
    return sorted(set(pads))


def check_pin_count(symbol_text: str, datasheet_pin_count: int,
                    footprint_text: Optional[str] = None):
    """MASTER_RULEBOOK Faz 1's pin-count gate, as a ``Finding``.

    Two comparisons:
      1. symbol pin count == datasheet pin count;
      2. footprint pad count == symbol pin count, when a footprint is given.

    The second one is the one that gets skipped in practice and the one that
    costs a board: a symbol from the exposed-pad variant next to a footprint
    from the one without it passes ERC and DRC cleanly.

    A symbol with no pins reports NO_COVERAGE - "0 pins, 0 violations, PASS"
    is unreachable through ``common.findings``.
    """
    pins = symbol_pin_numbers(symbol_text)
    violations: List[str] = []

    if len(pins) != datasheet_pin_count:
        violations.append(
            f"sembol_vs_datasheet: sembolde {len(pins)} pin, datasheet {datasheet_pin_count} "
            f"({len(pins) - datasheet_pin_count:+d})")

    if footprint_text is not None:
        pads = footprint_pad_numbers(footprint_text)
        if len(pads) != len(pins):
            missing_in_symbol = sorted(set(pads) - set(pins))
            missing_in_footprint = sorted(set(pins) - set(pads))
            violations.append(
                f"footprint_vs_sembol: {len(pads)} pad / {len(pins)} pin; "
                f"sembolde olmayan pad={missing_in_symbol}, "
                f"footprintte olmayan pin={missing_in_footprint}")

    return result("cad_varlik_pin_sayisi", len(pins), violations,
                  f"datasheet={datasheet_pin_count} pin, sembol={len(pins)} pin")


# --------------------------------------------------------------------------- #
# 3. Registration - refuses an unproven asset
# --------------------------------------------------------------------------- #
def add_lib_table_entry(table_path: str, nick: str, uri: str,
                        descr: str = "cad_assets ile eklendi") -> bool:
    """``True`` added, ``False`` already present. IDEMPOTENT on purpose:
    a duplicate nickname makes KiCad refuse to open the project, so the
    existence check is a safety rule and not a convenience.

    A missing table is created with a valid skeleton; the root node
    (``sym_lib_table`` / ``fp_lib_table``) follows the file name.
    """
    path = Path(table_path)
    root = "fp_lib_table" if "fp-lib-table" in path.name else "sym_lib_table"

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"({root}\n  (version 7)\n)\n", encoding="utf-8")

    content = path.read_text(encoding="utf-8")
    if f'(name "{nick}")' in content:
        return False

    closing = content.rfind(")")
    if closing == -1:
        raise ValueError(f"{table_path} geçerli bir lib-table dosyası değil.")
    path.write_text(content[:closing] + _LIB_LINE.format(nick=nick, uri=uri, descr=descr)
                    + content[closing:], encoding="utf-8")
    return True


def register_assets(project_dir: str, lib_name: str, symbol_path: Optional[str] = None,
                    footprint_path: Optional[str] = None, model_path: Optional[str] = None,
                    *, pin_check_passed: bool) -> dict:
    """Copies the assets into the project and registers the libraries.

    ``pin_check_passed=False`` raises ``PermissionError``. If the gate can
    be stepped around by a caller in a hurry, the gate does not exist.
    """
    if not pin_check_passed:
        raise PermissionError(
            "pin sayısı kapısı (check_pin_count) GEÇİLMEDEN kütüphaneye kayıt yapılamaz "
            "- MASTER_RULEBOOK Faz 1.")

    root = Path(project_dir)
    registered: List[str] = []
    copied: List[str] = []

    if symbol_path:
        target = root / f"{lib_name}.kicad_sym"
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(symbol_path).resolve() != target.resolve():
            shutil.copy(symbol_path, target)
        copied.append(str(target))
        if add_lib_table_entry(str(root / "sym-lib-table"), lib_name,
                               f"${{KIPRJMOD}}/{target.name}"):
            registered.append("sym-lib-table")

    if footprint_path:
        pretty = root / f"{lib_name}.pretty"
        pretty.mkdir(parents=True, exist_ok=True)
        target = pretty / Path(footprint_path).name
        if Path(footprint_path).resolve() != target.resolve():
            shutil.copy(footprint_path, target)
        copied.append(str(target))
        if add_lib_table_entry(str(root / "fp-lib-table"), lib_name,
                               f"${{KIPRJMOD}}/{pretty.name}"):
            registered.append("fp-lib-table")

    if model_path:
        models = root / "3d_models"
        models.mkdir(parents=True, exist_ok=True)
        target = models / Path(model_path).name
        if Path(model_path).resolve() != target.resolve():
            shutil.copy(model_path, target)
        copied.append(str(target))

    missing = [kind for kind, given in
               (("kicad_sym", symbol_path), ("kicad_mod", footprint_path), ("step", model_path))
               if not given]
    return {"library": lib_name, "copied": copied, "registered": registered, "missing": missing}


# --------------------------------------------------------------------------- #
# 4. CLI
# --------------------------------------------------------------------------- #
SAMPLE_SYMBOL = """(kicad_symbol_lib
  (symbol "U1"
    (property "Datasheet" "(number \\"99\\")")
    (symbol "U1_1_1"
      (pin passive line (at 0 0 0) (name "A") (number "1" (effects)))
      (pin power_in line (at 0 0 0) (name "B") (number "2"))
      (pin passive line (at 0 0 0) (name "EP") (number "EP")))
    (symbol "U1_1_2"
      (pin passive line (at 0 0 0) (name "A") (number "1")))))
"""
SAMPLE_FOOTPRINT = """(footprint "PKG"
  (pad "1" smd rect (at 0 0))
  (pad "2" smd rect (at 1 0))
  (pad "EP" smd rect (at 0.5 0.5)))
"""


def _self_test() -> int:
    assert symbol_pin_numbers(SAMPLE_SYMBOL) == ["1", "2", "EP"], symbol_pin_numbers(SAMPLE_SYMBOL)
    assert footprint_pad_numbers(SAMPLE_FOOTPRINT) == ["1", "2", "EP"]

    ok = check_pin_count(SAMPLE_SYMBOL, 3, SAMPLE_FOOTPRINT)
    assert ok["status"] == "PASS", ok

    wrong = check_pin_count(SAMPLE_SYMBOL, 8)
    assert wrong["status"] == "FAIL", wrong

    no_ep = """(footprint "PKG" (pad "1" smd rect (at 0 0)) (pad "2" smd rect (at 1 0)))"""
    mismatch = check_pin_count(SAMPLE_SYMBOL, 3, no_ep)
    assert mismatch["status"] == "FAIL", "eksik exposed pad yakalanmalı"

    # A mounting hole is (pad "" np_thru_hole ...): the absence of a number,
    # not a pad number. It must not turn a correct footprint into a FAIL.
    with_hole = SAMPLE_FOOTPRINT.rstrip()[:-1] + '\n  (pad "" np_thru_hole circle (at 2 2)))\n'
    assert footprint_pad_numbers(with_hole) == ["1", "2", "EP"], footprint_pad_numbers(with_hole)
    assert check_pin_count(SAMPLE_SYMBOL, 3, with_hole)["status"] == "PASS", \
        "montaj deliği sahte FAIL üretmemeli"

    empty = check_pin_count("(kicad_symbol_lib)", 0)
    assert empty["status"] == "NO_COVERAGE", empty

    blocked, reason = lifecycle_gate("ESP32-C3FN4", "EOL")
    assert not blocked and "İstisnasız" in reason
    allowed, reason = lifecycle_gate("X", None)
    assert allowed and "CONFIRM" in reason

    assert download_allowed("https://api.snapeda.com/x.kicad_sym")[0]
    assert not download_allowed("http://api.snapeda.com/x.kicad_sym")[0]
    assert not download_allowed("https://evil.example/x.kicad_sym")[0]

    try:
        register_assets(".", "lib", pin_check_passed=False)
        raise AssertionError("kapı atlanabiliyor - bu bir kapı değil")
    except PermissionError:
        pass

    diag("SELF-TEST PASS: structural pin/pad reading, pin-count gate, lifecycle gate, "
        "host allowlist and the registration refusal all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol", nargs="?", help="downloaded .kicad_sym")
    ap.add_argument("--datasheet-pins", type=int)
    ap.add_argument("--footprint")
    ap.add_argument("--model")
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--project")
    ap.add_argument("--lib-name", default="project_cad_assets")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.symbol:
        ap.error("symbol file is required unless --self-test is given")
    if a.datasheet_pins is None:
        ap.error("--datasheet-pins is required: the gate compares against the DATASHEET, "
                 "and a default would compare the symbol with itself")

    symbol_text = Path(a.symbol).read_text(encoding="utf-8")
    footprint_text = Path(a.footprint).read_text(encoding="utf-8") if a.footprint else None
    finding = check_pin_count(symbol_text, a.datasheet_pins, footprint_text)

    report = {"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME, "symbol": a.symbol,
              "footprint": a.footprint, "findings": [finding],
              "summary": summarise([finding])}

    if a.register:
        if not a.project:
            ap.error("--register requires --project")
        report["registration"] = register_assets(
            a.project, a.lib_name, a.symbol, a.footprint, a.model,
            pin_check_passed=finding["status"] == "PASS")

    write_report(report, a.json)
    emit(report)
    return 0 if finding["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
