#!/usr/bin/env python3
"""GÜVENLİK KİLİDİ — şeması ve testi olmayan araç sisteme giremez.

    python -m pytest tests/test_skill_contracts.py -q
    python tests/test_skill_contracts.py            # kapı modu (boru hattı)

NEDEN VAR (2026-08-11 GAP analizi, Sütun 3)
-------------------------------------------
Depoda `pcb-agent/skills/` altında YAML sözleşmeli 18 beceri vardı ama kök
`.claude/skills/` **boştu**: harness oraya bakar, dolayısıyla 18 beceri belge
olarak vardı, **yetenek olarak yoktu**. Aynı anda `tool_registry.json`'daki 90
kaydın hiçbirinde `input_schema` / `output_schema` / `tests` alanı yoktu — yani
hangi aracın VAR OLDUĞU biliniyordu, hangi VERİYİ TÜKETTİĞİ bilinmiyordu.

Deponun kendi denetim raporu bunu "en yüksek kaldıraç" diye işaretlemişti:
*"Registry'ye üç alan ekle — input_schema, output_schema, tests — ve
sync_tool_registry.py testsiz kayıtlı bir muhafız görünce RED dönsün."*
Bu dosya o kuralın makine karşılığıdır.

BU TESTİN YAKALADIĞI İNCELİK
----------------------------
`test_tool_registry.py` zaten "her skill'in andığı araç kayıtlı mı" ve "her
kayıtlı araca bir skill'den erişiliyor mu" diye soruyor — ve üç sert muhafız o
testi GEÇİYORDU, çünkü bir SKILL.md onları anıyordu. Ama hiçbiri bir boru
hattından çağrılmıyordu ve hiçbirinin testi yoktu. **Skill'den anılmak
çağrılmak değildir; kayıtlı olmak sınanmış olmak değildir.** Bu dosya ikinci
soruyu sorar.

KAPSAM — FAZ 1 (bilerek dar)
----------------------------
142+ modülün tamamı bir anda sözleşmelenmez; kilit önce **şematik doğrulama**
ve **DFM/üretim** kategorilerinde ve **kart muhafızlarında** kurulur (aşağıdaki
`FOCUS`). Kapsam dışı araçlar sayılır ve RAPORLANIR — sessizce geçilmez, ki
"kilit kuruldu" cümlesi kapsamıyla birlikte okunsun.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
REGISTRY = REPO / "config" / "tool_registry.json"
SKILLS_SRC = REPO / "skills"
SKILLS_DST = REPO / ".claude" / "skills"

# --------------------------------------------------------------------------- #
# FAZ 1 KAPSAMI. Bu listeye giren her araç `input_schema` VE `tests` taşımak
# ZORUNDADIR. Liste elle tutulur ve BÜYÜR — küçülmesi bir gerileme sayılır
# (`test_focus_set_does_not_shrink`).
# --------------------------------------------------------------------------- #
FOCUS_DFM = ["dfm-emc-check", "mask-dam", "stitch-density", "mechanical-dxf",
             "footprint-factory", "dru-emit"]
FOCUS_SCH = ["i2c-pullup", "rewire", "wireify"]
FOCUS_GUARDS = ["route-audit", "verify-zones", "verify-placement",
                "verify-manifest", "interconnect-mating"]
FOCUS = FOCUS_DFM + FOCUS_SCH + FOCUS_GUARDS


def registry() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def by_name() -> dict[str, dict]:
    return {t["name"]: t for t in registry()["tools"]}


def resolve_contract(ref: str) -> tuple[bool, str]:
    """Sözleşme referansı GERÇEKTEN çözülüyor mu? -> (ok, aciklama)

    İki biçim kabul edilir ve İKİSİ DE DİSKTE DOĞRULANIR:

      * ``schemas/xxx.schema.json``     — dosya var mı, geçerli JSON mu
      * ``scripts/a/b.py:ClassName``    — modülde o adda bir TypedDict/dataclass
                                          TANIMI var mı (AST ile; import ETMEZ)

    İkinci biçim neden AST ile: aracı import etmek yan etki çalıştırabilir
    (argparse, dosya okuma, pcbnew). Sözleşmenin VARLIĞINI sınamak için modülü
    çalıştırmak gerekmez ve çalıştırmak testi kırılgan yapar.
    """
    if ":" in ref:
        rel, cls = ref.rsplit(":", 1)
        p = TOOLS / rel
        if not p.is_file():
            return False, f"modul yok: {rel}"
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return False, f"{rel} ayristirilamadi: {exc}"
        classes = {n.name: n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef)}
        if cls not in classes:
            return False, f"{rel} icinde '{cls}' tanimi YOK"

        # KALITIM ZINCIRI TAKIP EDILIR. Yaygin desen:
        #     class XInputOptional(TypedDict, total=False): ...
        #     class XInput(XInputOptional): ...      # zorunlu alanlar
        # Yalniz DOGRUDAN tabanlara bakmak, bu desenle yazilmis gecerli bir
        # sozlesmeyi "TypedDict degil" diye reddederdi.
        seen: set[str] = set()
        stack = [cls]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            node = classes.get(cur)
            if node is None:
                continue
            decs = {getattr(d, "id", getattr(d, "attr", ""))
                    for d in node.decorator_list}
            if "dataclass" in decs:
                return True, f"{rel}:{cls}"
            for b in node.bases:
                nm = getattr(b, "id", getattr(b, "attr", ""))
                if nm in ("TypedDict", "BaseModel"):
                    return True, f"{rel}:{cls}"
                stack.append(nm)
        return False, (f"{rel}:{cls} var ama kalitim zinciri TypedDict/BaseModel/"
                       "dataclass'a varmiyor - makinece okunabilir sozlesme sayilmaz")
    p = TOOLS / ref
    if not p.is_file():
        return False, f"sema dosyasi yok: {ref}"
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        return False, f"{ref} gecerli JSON degil: {exc!r}"
    return True, ref


class SkillRegistration(unittest.TestCase):
    """Beceriler harness'a BAĞLI mı?"""

    def setUp(self):
        if not SKILLS_DST.is_dir():
            self.skipTest("`.claude/skills/` yok: harness kopyası bu checkout'ta "
                          "üretilmemiş (`python tools/scripts/sync_skills.py`)")

    def test_every_skill_is_registered_in_the_harness(self):
        r = subprocess.run(
            [sys.executable, str(TOOLS / "scripts" / "sync_skills.py"), "--check"],
            capture_output=True, text=True)
        self.assertEqual(
            r.returncode, 0,
            "Beceriler harness'a bagli degil ya da ayrismis. "
            "`python tools/scripts/sync_skills.py` ile senkronla.\n" + r.stdout)

    def test_harness_copy_count_matches_source(self):
        src = {d.name for d in SKILLS_SRC.iterdir()
               if (d / "SKILL.md").is_file()}
        self.assertTrue(src, "kaynak agacta hic beceri yok - glob yanlis")
        dst = {d.name for d in SKILLS_DST.iterdir() if d.is_dir()} \
            if SKILLS_DST.is_dir() else set()
        self.assertEqual(src, dst,
                         f"harness ile kaynak ayristi: eksik={sorted(src - dst)} "
                         f"fazla={sorted(dst - src)}")


class SkillDeclaredArtifacts(unittest.TestCase):
    """Skill'in BEYAN ETTİĞİ şema dosyaları diskte var mı?

    GAP raporunun tespiti: skill↔araç ADI çift yönlü sınanıyordu, ama
    `inputs:`/`outputs:` bloğundaki ŞEMA YOLLARI hiç sınanmıyordu — eksik
    `design_intent.schema.json` tam bu boşluktan sızmıştı.
    """

    def test_declared_schema_paths_exist(self):
        import re
        missing: list[str] = []
        pat = re.compile(r"schema:\s*([^\s,}]+)")
        for skill in sorted(SKILLS_SRC.glob("*/SKILL.md")):
            head = skill.read_text(encoding="utf-8").split("---")[1] \
                if skill.read_text(encoding="utf-8").startswith("---") else ""
            for ref in pat.findall(head):
                ref = ref.strip().strip('"').strip("'")
                if ref in ("null", "None", ""):
                    continue
                if not (skill.parent / ref).resolve().is_file():
                    missing.append(f"{skill.parent.name}: {ref}")
        self.assertEqual(missing, [],
                         "Skill bir sema BEYAN ediyor ama dosya diskte yok:\n  "
                         + "\n  ".join(missing))


class RegistryContractLock(unittest.TestCase):
    """ASIL KİLİT: şeması veya testi olmayan araç registry'de duramaz."""

    def test_registry_carries_the_three_contract_fields(self):
        for t in registry()["tools"]:
            if t.get("kind") != "local":
                continue
            for f in ("input_schema", "output_schema", "tests"):
                self.assertIn(
                    f, t,
                    f"{t['name']}: '{f}' alani YOK. Alan bosken bile `null` "
                    "yazilir - 'sozlesmesi yok' bir bilgidir ve gorunmelidir.")

    def test_focus_tools_are_registered(self):
        reg = by_name()
        missing = [n for n in FOCUS if n not in reg]
        self.assertEqual(missing, [],
                         f"Faz 1 kapsamindaki arac registry'de YOK: {missing}")

    def test_focus_tools_declare_a_resolvable_input_schema(self):
        reg, bad = by_name(), []
        for n in FOCUS:
            t = reg.get(n) or {}
            ref = t.get("input_schema")
            if not ref:
                bad.append(f"{n}: input_schema YOK")
                continue
            ok, why = resolve_contract(ref)
            if not ok:
                bad.append(f"{n}: {why}")
        self.assertEqual(bad, [],
                         "Sozlesmesi olmayan ya da COZULMEYEN arac:\n  "
                         + "\n  ".join(bad))

    def test_focus_tools_declare_tests_that_exist(self):
        reg, bad = by_name(), []
        for n in FOCUS:
            t = reg.get(n) or {}
            tests = t.get("tests")
            if not tests:
                bad.append(f"{n}: tests YOK")
                continue
            for rel in tests:
                if not (TOOLS / rel).is_file():
                    bad.append(f"{n}: beyan edilen test dosyasi yok -> {rel}")
        self.assertEqual(bad, [],
                         "Testi beyan edilmemis ya da beyani TUTMAYAN arac:\n  "
                         + "\n  ".join(bad))

    def test_declared_tests_are_not_empty(self):
        """Test dosyasi VAR ama icinde test YOKSA beyan bosa dusmustur."""
        reg, bad = by_name(), []
        for n in FOCUS:
            for rel in (reg.get(n) or {}).get("tests") or []:
                p = TOOLS / rel
                if not p.is_file():
                    continue
                tree = ast.parse(p.read_text(encoding="utf-8"))
                n_tests = sum(
                    1 for x in ast.walk(tree)
                    if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and x.name.startswith("test"))
                if n_tests == 0:
                    bad.append(f"{n}: {rel} icinde 'test' ile baslayan fonksiyon YOK")
        self.assertEqual(bad, [], "Bos test beyani:\n  " + "\n  ".join(bad))

    def test_focus_set_does_not_shrink(self):
        """Kapsam KUCULMEZ. Bir araci listeden cikarmak kilidi gevsetmektir;
        bu test o hamleyi gorunur kilar."""
        self.assertGreaterEqual(
            len(FOCUS), 13,
            "FAZ 1 kapsami kuculmus. Kapsam genisleyebilir, DARALAMAZ.")


def gate() -> int:
    """Boru hattı kapı modu: testleri koştur, RED'de 1 dön."""
    r = subprocess.run([sys.executable, "-m", "unittest", "-v",
                        "tests.test_skill_contracts"],
                       cwd=str(TOOLS), capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    print(out.strip())
    reg = registry()["tools"]
    local = [t for t in reg if t.get("kind") == "local"]
    with_c = [t for t in local if t.get("input_schema")]
    with_t = [t for t in local if t.get("tests")]
    print("\n[0] kayitli arac (local)        :", len(local))
    print(f"[1] input_schema tasiyan       : {len(with_c)}")
    print(f"[2] tests tasiyan              : {len(with_t)}")
    print(f"[3] FAZ 1 kapsami              : {len(FOCUS)} arac "
          f"({len(FOCUS_DFM)} DFM + {len(FOCUS_SCH)} sematik + "
          f"{len(FOCUS_GUARDS)} muhafiz)")
    print(f"[4] KAPSAM DISI (Faz 2)        : {len(local) - len(FOCUS)} arac - "
          "sozlesme kilidi HENUZ uygulanmiyor")
    print("\nSOZLESME MUHAFIZI:", "GREEN" if r.returncode == 0 else "RED")
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(gate())
