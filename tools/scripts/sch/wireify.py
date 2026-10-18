"""wireify — "uçan net" şematiğini gerçek wire'lı hale getirir.

Sorun: bazı üretici script'ler label'ı doğrudan pin ankrajının üstüne koyar.
Netlist doğru çıkar ama şematikte HİÇ ÇİZGİ YOKTUR: insan denetleyemez,
review edilemez, hiyerarşi okunmaz (bkz. usb-hs-breakout: 0 wire / 27 label).

Bu araç her pin-üstü label'ı pin'den `dist` kadar dışarı taşır ve arasına
`(wire ...)` koyar. **Netlist değişmemelidir** — script bunu kendisi
kicad-cli ile önce/sonra karşılaştırarak kanıtlar.

    python3 wireify.py board.kicad_sch            # dry-run (rapor)
    python3 wireify.py board.kicad_sch --write    # .bak + yaz + netlist kanıtı
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.ids import det_uuid  # noqa: E402
from common.kicad_cli import assert_kicad_closed, netlist_nets  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402
from common.sexpr import (  # noqa: E402
    STUB,
    block as _block,
    find,
    find_all,
    num,
    parse_sexpr,
    snap,
    unquote,
)
from sch_wire import Placed, _xform, load_lib_symbols  # noqa: E402


# --------------------------------------------------------------------------- #
# AI sözleşmesi: aracın girdi/çıktı yüzeyi. Burası DAVRANIŞ DEĞİŞTİRMEZ -
# çalışan koddan TÜRETİLMİŞ bir betimlemedir; alanlar okunarak yazıldı,
# uydurulmadı.
# --------------------------------------------------------------------------- #
class WireifyInput(TypedDict, total=False):
    """wireify girdi sözleşmesi.

    NEREDEN TÜREDİ: ``main()`` içindeki argparse bloğu —
    scripts/sch/wireify.py:228-235. Alan adları argparse ``Namespace``
    adlarıdır.

    ``sch`` tek zorunlu alandır (konumsal argüman); ``total=False`` bu yüzden -
    kalanların argparse'ta varsayılanı vardır (``write``/``force`` False,
    ``dist`` = ``common.sexpr.STUB``, ``json`` None).

    ``dist``, etiketin pin ankrajından ne kadar dışarı taşınacağıdır (mm);
    araya tam o uzunlukta bir ``(wire ...)`` konur. ``force`` yalnız
    "KiCad açık mı" kontrolünü atlar ve SADECE scratch kopyada kullanılmalıdır.

    Çıkış kodları (``main()`` satır 241-244 + ``common/report_io.run_cli``):
    0 = başarı · 1 = ``--write`` verildi ama netlist DEĞİŞTİ (dosya
    ``.bak``'tan geri alındı) · 2 = beklenmedik istisna.
    """

    sch: str
    write: bool
    dist: float
    force: bool
    json: Path


class WireifyOutput(TypedDict, total=False):
    """wireify rapor sözleşmesi (``schema_version`` = ``sch-wireify-report/v1``).

    NEREDEN TÜREDİ: iki yerden birleşir - ``wireify()`` içinde kurulan
    ``report`` sözlüğü (scripts/sch/wireify.py:182-184 ve 209-213) ile
    ``main()`` içinde onun üstüne yazılan zarf (satır 237-238). ``schemas/``
    altında ``sch-wireify-report/v1`` adında bir JSON Schema dosyası YOKTUR;
    sözleşmenin tek kaynağı bu iki noktadır.

    HER KOŞUDA bulunanlar: ``schema_version``, ``tool``, ``sch``, ``wrote``,
    ``labels_on_pins``, ``labels_elsewhere``, ``wires_added``, ``wires_before``.
    YALNIZ ``--write`` ile: ``netlist_identical`` ve ``netlist_diff``.
    YALNIZ geri alma olduysa: ``rolled_back``. ``total=False`` bunun içindir.

    Çıkış kodları: 0 = başarı · 1 = ``--write`` sonrası netlist değişti (geri
    alındı) · 2 = beklenmedik istisna.
    """

    schema_version: str
    tool: str
    sch: str                  # işlenen şematiğin yolu (main(), satır 238)
    wrote: bool               # --write verildi mi
    labels_on_pins: int       # tam pin ankrajının üstünde bulunan etiket sayısı
    labels_elsewhere: int     # pin üstünde OLMAYAN (dokunulmayan) etiket sayısı
    wires_added: int          # eklenen (wire ...) sayısı
    wires_before: int         # dosyada zaten var olan tel sayısı
    netlist_identical: bool   # yazım öncesi/sonrası netlist AYNI mı
    netlist_diff: list        # aynı değilse ayrışan net adları
    rolled_back: bool         # yalnız .bak'tan geri alındıysa vardır


LABEL_RE = re.compile(r'\((global_label|hierarchical_label|label) "')
SYMBOL_RE = re.compile(r'^\t\(symbol\n', re.M)


def _sym_placements(text: str, libs: dict) -> list[Placed]:
    out = []
    for m in SYMBOL_RE.finditer(text):
        raw = _block(text, m.start())
        t = parse_sexpr(raw)
        lib_id_node = find(t, "lib_id")
        at = find(t, "at")
        if not (lib_id_node and at):
            continue
        lib_id = unquote(lib_id_node[1])
        ref = None
        for p in find_all(t, "property"):
            if unquote(p[1]) == "Reference":
                ref = unquote(p[2])
        mirror = find(t, "mirror")
        unit = find(t, "unit")
        sym = libs.get(lib_id)
        if sym is None or ref is None:
            continue
        out.append(Placed(ref, lib_id, float(at[1]), float(at[2]),
                          float(at[3]) if len(at) > 3 else 0.0,
                          mirror[1] if mirror else "",
                          int(unit[1]) if unit else 1, sym))
    return out


def wireify(path: str, dist: float = STUB, write: bool = False,
            force: bool = False) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    libs = load_lib_symbols(path)                       # gömülü lib_symbols
    anchors: dict[tuple[float, float], object] = {}
    for pl in _sym_placements(text, libs):
        for pin in pl.sym.pins:
            if pin.unit not in (0, pl.unit):
                continue
            try:
                pr = pl.pin(pin.number)
            except KeyError:
                continue
            anchors[(round(pr.x, 2), round(pr.y, 2))] = pr

    edits, wires = [], []
    hits = misses = 0
    for m in LABEL_RE.finditer(text):
        raw = _block(text, m.start())
        t = parse_sexpr(raw)
        at = find(t, "at")
        if not at:
            continue
        key = (round(float(at[1]), 2), round(float(at[2]), 2))
        pr = anchors.get(key)
        if pr is None:
            misses += 1
            continue
        hits += 1
        nx, ny = pr.out(dist)
        # etiketi dışarı taşı; açıyı pin'in dış yönüne çevir
        old_at = f"(at {at[1]} {at[2]}" + (f" {at[3]}" if len(at) > 3 else "")
        new_at = f"(at {num(nx)} {num(ny)} {num(pr.out_angle)}"
        idx = raw.index(old_at)
        edits.append((m.start() + idx, m.start() + idx + len(old_at), new_at))
        wires.append((key, (nx, ny), f"{pr.ref}-{pr.pin.number}"))

    body = "\n".join(
        "\t(wire\n"
        f"\t\t(pts (xy {num(a[0])} {num(a[1])}) (xy {num(b[0])} {num(b[1])}))\n"
        "\t\t(stroke (width 0) (type default))\n"
        f"\t\t(uuid \"{_det_uuid(path, tag)}\")\n\t)"
        for a, b, tag in wires)

    report = {"labels_on_pins": hits, "labels_elsewhere": misses,
              "wires_added": len(wires),
              "wires_before": text.count("(wire\n") + text.count("(wire ")}

    if not write:
        return report

    assert_kicad_closed(path, force=force)  # force: SADECE scratch kopya için
    before = netlist_nets(path)
    new = text
    for s, e, rep in sorted(edits, reverse=True):
        new = new[:s] + rep + new[e:]
    tail = new.rstrip()
    assert tail.endswith(")")
    new = tail[:-1].rstrip("\n") + "\n" + body + "\n)\n"

    shutil.copyfile(path, path + ".bak")
    # write_text CLOSES the handle before we move on. A bare
    # open(...).write(...) leaves the write handle open until the garbage
    # collector runs, and on Windows an open handle makes the copyfile in
    # the rollback path below fail with PermissionError - i.e. the recovery
    # would break exactly when it is needed.
    Path(path).write_text(new, encoding="utf-8")
    after = netlist_nets(path)

    changed = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    report["netlist_identical"] = not changed
    report["netlist_diff"] = sorted(changed)
    if changed:                                          # güvenli geri alma
        shutil.copyfile(path + ".bak", path)
        report["rolled_back"] = True
    return report


def _det_uuid(path: str, tag: str) -> str:
    """Deterministic UUID in the ``wireify`` namespace."""
    return det_uuid("wireify", path, tag)


SCHEMA_VERSION = "sch-wireify-report/v1"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sch", help="target .kicad_sch")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--dist", type=float, default=STUB)
    ap.add_argument("--json", type=Path,
                    help="also write the JSON report to this path")
    ap.add_argument("--force", action="store_true",
                    help="KiCad-açık kontrolünü atla — SADECE scratch kopyada")
    a = ap.parse_args(argv)
    rep = wireify(a.sch, dist=a.dist, write=a.write, force=a.force)
    report = {"schema_version": SCHEMA_VERSION, "tool": "wireify",
              "sch": a.sch, "wrote": bool(a.write), **rep}
    write_report(report, a.json)
    emit(report)
    if a.write and not rep.get("netlist_identical"):
        diag("HATA: netlist değişti -> .bak'tan geri alındı")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
