#!/usr/bin/env python3
"""Bolge (zone) muhafizi: `zones[]` ETIKETI ile SOZLESMESI ayni mi?

NEDEN VAR — olculmus bir olay (2026-08-10, cam-w-og05b10):
`layout/board_outline.json` dosyasinin ilk satiri `"$schema": "cam-w/board_outline/v1"`
diyordu. `pcb-agent/tools/schemas/` altinda bu adda BIR SEY YOKTU. Yani dosya bir
sozlesmeye ATIFTA BULUNUYOR ama sozlesme HIC YAZILMAMISTI: etiket var, karsilik yok.
Bu arada `zones[]` dizisini en az dort arac okuyordu (verify_placement, dfm_emc_check,
mechanical_dxf, force_directed). Dogrulanmayan bir sema, sema degil temennidir.

NE KONTROL EDER
  1. YAPI     — schemas/zones.schema.json (bicim, zorunlu alanlar, kapali `kind` listesi,
                rect_mm ile circle_mm'in birbirini disladigi).
  2. TUTARLILIK — JSON Schema'nin IFADE EDEMEDIGI, alanlar ARASI kurallar:
                  id benzersizligi, escape_layer'in exclude_layers icinde olmasi,
                  allowed_refs_why anahtarlarinin allowed_refs'in alt kumesi olmasi.

IKI MOTOR, TEK SOZLESME
`jsonschema` kutuphanesi varsa o kullanilir; yoksa bu dosyadaki mini dogrulayici
AYNI .json dosyasini yorumlar. Kurallar Python'a KOPYALANMAZ — kopyalansaydi sema ile
dogrulayici zamanla birbirinden ayrilir ve hangisinin dogru oldugu bilinemezdi.
`--no-jsonschema` bayragi yedek motoru zorlar (yedegin gercekten calistigini gormek icin).

Cikis: 0 GREEN · 1 ihlal var (RED) · 2 girdi okunamadi.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# schemas/ dizini: .../tools/scripts/board/verify_zones.py -> .../tools/schemas/
DEFAULT_SCHEMA = Path(__file__).resolve().parents[2] / "schemas" / "zones.schema.json"


# --------------------------------------------------------------------------- #
# Yedek motor: sema dosyasinda GERCEKTEN kullanilan JSON Schema alt kumesi.
# Bilinmeyen bir anahtar sessizce atlanmaz -> sema buyurse eksik kalmasi fark edilir.
# --------------------------------------------------------------------------- #
_IGNORED = {"$schema", "$id", "$defs", "title", "description", "$comment", "default",
            "examples"}
_SUPPORTED = {"type", "required", "properties", "additionalProperties", "items",
              "$ref", "enum", "pattern", "minLength", "minItems", "uniqueItems",
              "exclusiveMinimum", "minimum", "oneOf", "anyOf", "allOf", "not",
              "dependentRequired", "propertyNames", "const", "if", "then", "else",
              "minProperties"}

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None),
}


def _type_ok(val: Any, name: str) -> bool:
    if name == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if name == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if name == "boolean":
        return isinstance(val, bool)
    py = _TYPES.get(name)
    if py is None:
        return True                      # tanimadigimiz tur -> iddia etme
    if py is not bool and isinstance(val, bool):
        return False                     # Python'da True bir int'tir; sayi sanmayalim
    return isinstance(val, py)


def _resolve(ref: str, root: dict) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"yerel olmayan $ref desteklenmiyor: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def mini_validate(inst: Any, schema: dict, root: dict, path: str = "$") -> list[str]:
    """jsonschema yoksa kullanilan yedek motor. Sema dosyasini YORUMLAR."""
    err: list[str] = []

    unknown = set(schema) - _SUPPORTED - _IGNORED
    if unknown:
        err.append(f"{path}: YEDEK MOTOR EKSIK - sema anahtari desteklenmiyor: "
                   f"{sorted(unknown)} (jsonschema kurun ya da motoru guncelleyin)")

    if "$ref" in schema:
        err += mini_validate(inst, _resolve(schema["$ref"], root), root, path)

    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(inst, n) for n in names):
            err.append(f"{path}: tur {names} olmali, gelen {type(inst).__name__}")
            return err                    # tur yanlissa alt kontroller anlamsiz

    if "const" in schema and inst != schema["const"]:
        err.append(f"{path}: {schema['const']!r} olmali, gelen {inst!r}")
    if "enum" in schema and inst not in schema["enum"]:
        err.append(f"{path}: izinli degerler {schema['enum']}, gelen {inst!r}")

    if isinstance(inst, str):
        if "pattern" in schema and not re.search(schema["pattern"], inst):
            err.append(f"{path}: {inst!r} bicime uymuyor ({schema['pattern']})")
        if "minLength" in schema and len(inst) < schema["minLength"]:
            err.append(f"{path}: en az {schema['minLength']} karakter olmali")

    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "exclusiveMinimum" in schema and not inst > schema["exclusiveMinimum"]:
            err.append(f"{path}: {schema['exclusiveMinimum']} degerinden BUYUK olmali, "
                       f"gelen {inst}")
        if "minimum" in schema and inst < schema["minimum"]:
            err.append(f"{path}: en az {schema['minimum']} olmali, gelen {inst}")

    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            err.append(f"{path}: en az {schema['minItems']} oge olmali")
        if schema.get("uniqueItems") and len(inst) != len({json.dumps(i, sort_keys=True)
                                                           for i in inst}):
            err.append(f"{path}: ogeler benzersiz olmali (tekrar var)")
        if "items" in schema:
            for i, it in enumerate(inst):
                err += mini_validate(it, schema["items"], root, f"{path}[{i}]")

    if isinstance(inst, dict):
        if "minProperties" in schema and len(inst) < schema["minProperties"]:
            err.append(f"{path}: en az {schema['minProperties']} alan olmali, "
                       f"gelen {len(inst)}")
        for k in schema.get("required", []):
            if k not in inst:
                err.append(f"{path}: zorunlu alan EKSIK -> '{k}'")
        props = schema.get("properties", {})
        for k, v in inst.items():
            if k in props:
                err += mini_validate(v, props[k], root, f"{path}.{k}")
        extra = schema.get("additionalProperties")
        if extra is not None and extra is not True:
            unlisted = [k for k in inst if k not in props]
            if extra is False:
                for k in sorted(unlisted):
                    err.append(f"{path}: sozlesmede OLMAYAN alan -> '{k}' "
                               "(yazim hatasi mi, yoksa sema mi guncellenmeli?)")
            else:
                for k in sorted(unlisted):
                    err += mini_validate(inst[k], extra, root, f"{path}.{k}")
        if "propertyNames" in schema:
            for k in sorted(inst):
                err += mini_validate(k, schema["propertyNames"], root,
                                     f"{path}: anahtar '{k}'")
        for k, deps in schema.get("dependentRequired", {}).items():
            if k in inst:
                for d in deps:
                    if d not in inst:
                        err.append(f"{path}: '{k}' varsa '{d}' de ZORUNLU")

    if "not" in schema and not mini_validate(inst, schema["not"], root, path):
        err.append(f"{path}: 'not' kosulu ihlal edildi")
    for sub in schema.get("allOf", []):
        err += mini_validate(inst, sub, root, path)
    if "anyOf" in schema and not any(
            not mini_validate(inst, s, root, path) for s in schema["anyOf"]):
        err.append(f"{path}: anyOf dallarindan HICBIRI tutmadi")
    if "oneOf" in schema:
        hit = [i for i, s in enumerate(schema["oneOf"])
               if not mini_validate(inst, s, root, path)]
        if len(hit) != 1:
            err.append(f"{path}: oneOf tam OLARAK bir dal tutmali, tutan {len(hit)} "
                       "(rect_mm ile circle_mm birbirini DISLAR: tam biri olmali)")
    # if/then/else — kind'a bagli kosullu zorunluluklar icin (orn. yalniz
    # connector_band, kacis y'sinin turetilebilmesi icin connector_ref ve
    # escape_margin_mm tasimak zorundadir). Draft 2020-12 semantigi: `if`
    # BASARISIZ olursa bu bir hata DEGILDIR, sadece `then` uygulanmaz.
    if "if" in schema:
        branch = "then" if not mini_validate(inst, schema["if"], root, path) else "else"
        if branch in schema:
            err += mini_validate(inst, schema[branch], root, path)
    return err


# --------------------------------------------------------------------------- #
def structural_errors(doc: Any, schema: dict, use_jsonschema: bool) -> tuple[list[str], str]:
    """(hatalar, motor adi)."""
    if use_jsonschema:
        try:
            import jsonschema                                  # noqa: PLC0415
        except ImportError:
            use_jsonschema = False
    if not use_jsonschema:
        return mini_validate(doc, schema, schema), "mini (yedek)"

    import importlib.metadata                                  # noqa: PLC0415

    import jsonschema                                          # noqa: PLC0415
    validator = jsonschema.Draft202012Validator(schema)
    out = []
    for e in sorted(validator.iter_errors(doc), key=lambda x: list(x.absolute_path)):
        loc = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}"
                            for p in e.absolute_path)
        out.append(f"{loc}: {e.message}")
    try:
        ver = importlib.metadata.version("jsonschema")
    except Exception:                                          # noqa: BLE001
        ver = "?"
    return out, f"jsonschema {ver}"


def semantic_errors(doc: Any) -> list[str]:
    """JSON Schema'nin IFADE EDEMEDIGI, alanlar arasi tutarlilik kurallari."""
    err: list[str] = []
    zones = doc.get("zones") if isinstance(doc, dict) else None
    if not isinstance(zones, list):
        return err                        # yapi kontrolu zaten bagirdi

    seen: dict[str, int] = {}
    for i, z in enumerate(zones):
        if not isinstance(z, dict):
            continue
        zid = z.get("id")
        if isinstance(zid, str):
            if zid in seen:
                err.append(f"$.zones[{i}]: id '{zid}' zaten zones[{seen[zid]}] icinde "
                           "kullanildi - ihlal mesajlari hangi bolgeye ait ayirt edilemez")
            else:
                seen[zid] = i

        esc, excl = z.get("escape_layer"), z.get("exclude_layers")
        if isinstance(esc, str) and isinstance(excl, list) and esc not in excl:
            err.append(f"$.zones[{i}] ({zid}): escape_layer '{esc}' exclude_layers "
                       f"{excl} icinde DEGIL - kacis izi o katmanda kosuyorsa o "
                       "katmanda bilesen de duramaz, keepout OLU kalir")

        why, allowed = z.get("allowed_refs_why"), z.get("allowed_refs")
        if isinstance(why, dict) and isinstance(allowed, list):
            for ref in sorted(set(why) - set(allowed)):
                err.append(f"$.zones[{i}] ({zid}): allowed_refs_why['{ref}'] bir "
                           "gerekce yaziyor ama '{0}' allowed_refs icinde YOK - var "
                           "olmayan bir izin savunuluyor".format(ref))
    return err


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--outline", required=True,
                    help="zones[] tasiyan JSON (ornek: layout/board_outline.json)")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA),
                    help=f"sozlesme dosyasi (varsayilan: {DEFAULT_SCHEMA})")
    ap.add_argument("--no-jsonschema", action="store_true",
                    help="jsonschema kurulu olsa bile yedek motoru zorla")
    ap.add_argument("--max-print", type=int, default=25,
                    help="basilacak azami ihlal sayisi (varsayilan 25)")
    a = ap.parse_args(argv)

    try:
        doc = json.loads(Path(a.outline).read_text(encoding="utf-8"))
        schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        print(f"[zone] girdi okunamadi: {exc!r}")
        return 2

    try:
        struct, engine = structural_errors(doc, schema, not a.no_jsonschema)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[zone] sozlesme yorumlanamadi: {exc!r}")
        return 2

    sem = semantic_errors(doc)
    n_zones = len(doc.get("zones", [])) if isinstance(doc, dict) else 0

    print(f"[0] sozlesme               : {Path(a.schema).name}  (motor: {engine})")
    print(f"[1] dosya                  : {a.outline}")
    print(f"[2] zone sayisi            : {n_zones}")
    if isinstance(doc, dict) and isinstance(doc.get("zones"), list) and n_zones == 0:
        print("      UYARI: zones[] BOS. 'bolge yok' ile 'bolge tanimlanmadi' ayni "
              "sey degildir; bu dosya hicbir yerlesim kisiti dayatmaz.")
    print(f"[3] yapi ihlali            : {len(struct)}")
    for m in struct[:a.max_print]:
        print("      ", m)
    if len(struct) > a.max_print:
        print(f"       ... {len(struct) - a.max_print} tane daha")
    print(f"[4] tutarlilik ihlali      : {len(sem)}")
    for m in sem[:a.max_print]:
        print("      ", m)

    if struct or sem:
        print("\nZONE MUHAFIZI: RED")
        return 1
    print("\nZONE MUHAFIZI: GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
