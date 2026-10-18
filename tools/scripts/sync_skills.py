#!/usr/bin/env python3
"""`pcb-agent/skills/` -> `.claude/skills/` — becerileri harness'a BAĞLAR.

    python sync_skills.py            # materyalize et
    python sync_skills.py --check    # ayrışma var mı (kapı modu, ihlalde 1)

SORUN, ÖLÇÜLDÜ (2026-08-11 GAP analizi, Sütun 3)
------------------------------------------------
`pcb-agent/skills/` altında YAML sözleşmeli 18 SKILL.md vardı — `inputs`,
`outputs`, `writes_board`, `tools[]` alanlarıyla, 144-460 satır arasında.
Ama depo kökündeki `.claude/skills/` **BOŞTU**: harness'ın beceri yükleyicisi
oraya bakar, dolayısıyla bu 18 beceri **belge olarak vardı, yetenek olarak
yoktu**. Ajan hiçbirini çağıramıyordu.

NİYE KOPYA, NİYE SYMLINK DEĞİL
------------------------------
Windows'ta symlink yönetici hakkı ister, junction ise git'in taşıyamadığı bir
dosya sistemi nesnesidir. Kopya seçildi — ama kopya DRIFT demektir ve bu depo
drift'i sessizce taşımaz: `--check` iki tarafın ayrışmadığını sınar ve
`test_skill_contracts.py` bunu kapı olarak koşturur. Yani `.claude/skills/`
TÜRETİLMİŞ bir dizindir; kaynağı `pcb-agent/skills/`'tir ve elle düzenlenirse
bir sonraki senkronda geri alınır (aynı `netclasses.json` dersi).

FRONTMATTER ZORUNLU
-------------------
`name` ve `description` taşımayan bir SKILL.md harness'a KAYDEDİLMEZ: adsız
bir beceri çağrılamaz, açıklamasız bir beceri yanlış zamanda çağrılır. Böyle
bir dosya `--check` içinde ihlaldir, sessizce atlanmaz.

Çıkış: 0 senkron · 1 ayrışma/eksik frontmatter · 2 girdi okunamadı
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "skills"
DST = REPO / ".claude" / "skills"

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def frontmatter(text: str) -> dict[str, str]:
    """SKILL.md ust bilgisinden `name` ve `description`. Tam YAML DEGIL:
    yalniz bu iki alan gerekiyor ve tam ayristirici bagimlilik ekler."""
    m = _FM.match(text)
    if not m:
        return {}
    out: dict[str, str] = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^(name|description)\s*:\s*(.+?)\s*$", line)
        if mm:
            out[mm.group(1)] = mm.group(2).strip().strip('"').strip("'")
    return out


def discover() -> list[tuple[str, Path, dict]]:
    """(ad, SKILL.md yolu, frontmatter) — kaynak agactaki her beceri."""
    if not SRC.is_dir():
        raise SystemExit(f"HATA: beceri kaynagi yok: {SRC}")
    out = []
    for d in sorted(SRC.iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue                     # `_shared/` gibi yardimci dizinler
        out.append((d.name, f, frontmatter(f.read_text(encoding="utf-8"))))
    return out


def _norm(p: Path) -> bytes:
    """Satir sonundan bagimsiz icerik (CRLF/LF farki ayrisma DEGILDIR)."""
    return p.read_bytes().replace(b"\r\n", b"\n")


def sync(check: bool) -> int:
    skills = discover()
    problems: list[str] = []
    synced = missing = stale = 0

    for name, src, fm in skills:
        if not fm.get("name") or not fm.get("description"):
            problems.append(f"{name}: frontmatter'da "
                            f"{'name' if not fm.get('name') else 'description'} YOK "
                            "- adsiz/aciklamasiz beceri kaydedilmez")
            continue
        if fm["name"] != name:
            problems.append(f"{name}: frontmatter name='{fm['name']}' dizin adiyla "
                            "uyusmuyor - harness dizin adiyla arar")
        dst = DST / name / "SKILL.md"
        if not dst.exists():
            missing += 1
            if not check:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                synced += 1
            else:
                problems.append(f"{name}: harness'a KAYITLI DEGIL ({dst} yok)")
        elif _norm(dst) != _norm(src):
            stale += 1
            if not check:
                shutil.copy2(src, dst)
                synced += 1
            else:
                problems.append(f"{name}: harness kopyasi KAYNAKTAN AYRISMIS")

    # Kaynakta olmayan bir beceri harness'ta duruyorsa: yetim.
    src_names = {n for n, _, _ in skills}
    orphans = []
    if DST.is_dir():
        for d in sorted(DST.iterdir()):
            if d.is_dir() and d.name not in src_names:
                orphans.append(d.name)
                problems.append(f"{d.name}: harness'ta var, KAYNAKTA YOK (yetim)")

    print(f"[0] kaynak                 : {SRC}")
    print(f"[1] hedef (harness)        : {DST}")
    print(f"[2] kaynaktaki beceri      : {len(skills)}")
    print(f"[3] harness'ta eksik       : {missing}")
    print(f"[4] harness'ta bayat       : {stale}")
    print(f"[5] yetim (kaynakta yok)   : {len(orphans)}")
    if not check:
        print(f"[6] yazilan               : {synced}")
    for p in problems[:30]:
        print("      ", p)

    if check and problems:
        print("\nBECERI KAYIT MUHAFIZI: RED")
        return 1
    if problems and not check:
        # Yazma modunda bile frontmatter hatasi duzeltilemez.
        hard = [p for p in problems if "frontmatter" in p or "uyusmuyor" in p
                or "yetim" in p]
        if hard:
            print("\nBECERI KAYIT MUHAFIZI: RED (senkron yazildi ama su sorunlar "
                  "elle cozulmeli)")
            return 1
    print("\nBECERI KAYIT MUHAFIZI: GREEN")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="hicbir sey yazma; ayrisma varsa 1 don (KAPI MODU)")
    a = ap.parse_args(argv)
    try:
        return sync(a.check)
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        print(f"[beceri] girdi okunamadi: {exc!r}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
