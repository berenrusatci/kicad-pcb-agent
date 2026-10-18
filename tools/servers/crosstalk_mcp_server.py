#!/usr/bin/env python3
"""MCP stdio sunucusu — `analyze_crosstalk_and_thinning` tool'unu yayınlar.

backend: mcp/stdio  ·  bağımlılık: yalnız Python stdlib (mcp paketi GEREKMEZ)

Protokol: MCP 2024-11-05 JSON-RPC 2.0, satır-sınırlı (LSP framing YOK).
Desteklenen metotlar: initialize, notifications/initialized, tools/list,
tools/call, ping, shutdown.

Hesap motoru `crosstalk_thinning.py`'dir; bu dosya yalnız taşıma katmanıdır ve
hiçbir mühendislik kararı vermez. Model sınırları, `signoff: false` ve
`NO_COVERAGE` semantiği motorun sözleşmesidir — bkz. o dosyanın başlığı.

Elle deneme:
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 crosstalk_mcp_server.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_TOOLS / "scripts"))

from crosstalk_thinning import (  # noqa: E402
    SCHEMA_VERSION,
    InputError,
    analyze,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pcb-crosstalk"
SERVER_VERSION = "1.0.0"

TOOL_NAME = "analyze_crosstalk_and_thinning"

TOOL_DESCRIPTION = (
    "İki paralel PCB izi için geri-yönlü crosstalk (NEXT) tarar ve tek bir "
    "aksiyon döndürür: SAFE_TO_THIN (suggested_width_mm ile incelt), "
    "CROSSTALK_RISK (ASLA inceltme, suggested_spacing_mm kadar ayır), "
    "THINNING_BLOCKED (boşluk yeterli ama inceltme yasak) veya NO_COVERAGE "
    "(EM girdisi eksik — sessiz PASS yok). Kapalı-form tarama modelidir "
    "(model_class=exploratory_closed_form, signoff=false); kontrollü empedans "
    "ve hızlı arayüz imzası için field solver zorunludur. FEXT modellenmez."
)

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "trace_width_mm": {
            "type": "number", "exclusiveMinimum": 0,
            "description": "Mevcut iz genişliği W (mm).",
        },
        "spacing_mm": {
            "type": "number", "exclusiveMinimum": 0,
            "description": "İki iz arası KENAR-KENAR boşluk S (mm). Merkez-merkez değil.",
        },
        "dielectric_height_mm": {
            "type": "number", "exclusiveMinimum": 0,
            "description": (
                "En yakın referans düzlemine dielektrik yükseklik H (mm). "
                "Crosstalk kararının tek belirleyici stackup girdisi; "
                "verilmezse sonuç NO_COVERAGE olur."
            ),
        },
        "layer_type": {
            "type": "string", "enum": ["microstrip", "stripline"],
            "default": "microstrip",
            "description": "Dış katman (microstrip) veya iç katman (stripline).",
        },
        "coupled_length_mm": {
            "type": "number", "exclusiveMinimum": 0,
            "description": (
                "İki izin PARALEL gittiği uzunluk (mm). Doyum uzunluğunun "
                "altında NEXT doğrusal ölçeklenir."
            ),
        },
        "rise_time_ps": {
            "type": "number", "exclusiveMinimum": 0,
            "description": (
                "Saldırgan sinyalin kenar yükselme süresi (ps). Saat frekansı "
                "değil, KENAR HIZI belirleyicidir."
            ),
        },
        "er": {
            "type": "number", "exclusiveMinimum": 0, "default": 4.3,
            "description": "Dielektrik sabiti (FR-4 tipik 4.2–4.6).",
        },
        "copper_weight_oz": {
            "type": "number", "exclusiveMinimum": 0, "default": 1.0,
            "description": "Bakır ağırlığı (oz); iz kalınlığı için.",
        },
        "reference_plane_continuous": {
            "type": "boolean",
            "description": (
                "İzlerin altındaki referans düzlemi kesintisiz mi? false ise "
                "model geçersizdir ve sonuç NO_COVERAGE'a düşer."
            ),
        },
        "impedance_controlled": {
            "type": "boolean", "default": False,
            "description": (
                "Net kontrollü empedanslı mı? true ise inceltme kategorik "
                "olarak yasaktır (genişlik empedans çözücüsünün çıktısıdır)."
            ),
        },
        "noise_budget_ratio": {
            "type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1,
            "default": 0.05,
            "description": (
                "Kurban hatta izin verilen NEXT genliği, sinyal salınımına oran "
                "(0.05 = %5). Gerçek değer alıcı gürültü marjından gelir."
            ),
        },
        "current_a": {
            "type": "number", "exclusiveMinimum": 0,
            "description": "İzin taşıdığı sürekli akım (A); inceltme tabanı için.",
        },
        "delta_t_c": {
            "type": "number", "exclusiveMinimum": 0, "default": 10.0,
            "description": "İzin veriline sıcaklık artışı (°C).",
        },
        "fab_min_width_mm": {
            "type": "number", "exclusiveMinimum": 0, "default": 0.15,
            "description": "Üretici minimum iz genişliği (mm). Varsayılan 0.15 = 6 mil.",
        },
        "fab_min_clearance_mm": {
            "type": "number", "exclusiveMinimum": 0, "default": 0.15,
            "description": "Üretici minimum bakır boşluğu (mm).",
        },
        "net_class": {
            "type": "string",
            "description": "KiCad net-class adı (rapor izlenebilirliği için).",
        },
    },
    "required": ["trace_width_mm", "spacing_mm"],
    "additionalProperties": False,
}


def _summary(result: dict) -> str:
    """Ajanın tek bakışta aksiyon alabileceği kısa metin."""
    lines = [f"STATUS: {result['status']} ({result.get('status_reason', '-')})"]
    base = result.get("metrics", {}).get("baseline")
    if base:
        lines.append(
            f"NEXT: {base['next_percent']}%  (bütçe "
            f"{result['metrics']['noise_budget_ratio'] * 100:.2f}%)  "
            f"D/H={base['d_over_h']}  Z0~{base['z0_ohm']} Ω"
        )
    lines.append(
        f"AKSİYON: suggested_width_mm={result['suggested_width_mm']}  "
        f"suggested_spacing_mm={result['suggested_spacing_mm']}"
    )
    for b in result.get("blockers", []):
        lines.append(f"BLOCKER: {b}")
    for r in result.get("rationale", []):
        lines.append(f"- {r}")
    for c in result.get("confirm", []):
        lines.append(f"CONFIRM: {c}")
    lines.append(
        f"model_class={result.get('model_class')} signoff={result.get('signoff')} "
        f"coverage={json.dumps(result.get('coverage', {}))}"
    )
    return "\n".join(lines)


def _handle(method: str, params: dict):
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [{
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION,
                "inputSchema": INPUT_SCHEMA,
            }]
        }
    if method == "tools/call":
        name = params.get("name")
        if name != TOOL_NAME:
            raise LookupError(f"bilinmeyen tool: {name!r}")
        args = params.get("arguments") or {}
        unknown = set(args) - set(INPUT_SCHEMA["properties"])
        if unknown:
            raise InputError(f"bilinmeyen parametre: {sorted(unknown)}")
        result = analyze(**args)
        payload = {
            "content": [
                {"type": "text", "text": _summary(result)},
                {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
            ],
            "isError": False,
        }
        return payload
    raise LookupError(f"desteklenmeyen metot: {method!r}")


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(stdout, {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": f"parse error: {exc}"}})
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:                      # notification
            if method == "notifications/cancelled":
                continue
            continue

        try:
            result = _handle(method, params)
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})
        except InputError as exc:
            # Tool düzeyi hata: MCP'de protokol hatası değil, isError içeriktir.
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"schema_version": SCHEMA_VERSION, "status": "ERROR",
                     "error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            }})
        except LookupError as exc:
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32601, "message": str(exc)}})
        except Exception as exc:                # noqa: BLE001 — sunucu ölmemeli
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32603,
                                      "message": f"{type(exc).__name__}: {exc}"}})
        if method == "shutdown":
            break
    return 0


def _write(stream, obj) -> None:
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


if __name__ == "__main__":
    sys.exit(serve())
