#!/usr/bin/env python3
"""MCP server exposing the four READ-ONLY pcbnew board checkers.

backend: swig (pcbnew)  ·  transport only  ·  makes no engineering decision

Modelled on ``crosstalk_mcp_server.py``: pure-stdlib stdio JSON-RPC, MCP
protocol 2024-11-05, no ``mcp`` package. It is a SEPARATE process from the
crosstalk server on purpose - importing ``pcbnew`` requires the KiCad SWIG
environment, and the crosstalk server must stay stdlib-pure so it can run
anywhere.

EXPOSED (all read-only, none writes a board or schematic):
    dfm_emc_check       teardrops, via-in-pad, annular ring, EPAD paste,
                        edge keepout, HS return via, HS netclass
    mask_dam            solder-mask dam width in pin-to-pin channels
    stitch_density      lambda/20 edge and via-return stitching spacing
    via_stub            unused via barrel length + quarter-wave pre-screen

DELIBERATELY NOT EXPOSED:
    context_pack --postflight   executes arbitrary argv via subprocess; an
                                MCP-reachable postflight is remote code
                                execution driven by a JSON file
    rewire, wireify, sch_wire   they MUTATE schematics
    research_promotion_gate     writes into the vault, and the KABUL call
                                belongs to a human

PATH CONFINEMENT: the ``board`` argument is resolved under a root directory
(``PCB_AGENT_BOARD_ROOT``, default: current working directory) and must carry
a ``.kicad_pcb`` suffix. Without this an MCP client could ask the server to
load any file on disk.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TOOLS))
sys.path.insert(0, str(_TOOLS / "scripts"))
sys.path.insert(0, str(_TOOLS / "scripts" / "board"))

from common.findings import board_report  # noqa: E402
from common.paths import PathConfinementError, confine_to_dir  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pcb-board-checks"
SERVER_VERSION = "1.0.0"

BOARD_ROOT = Path(os.environ.get("PCB_AGENT_BOARD_ROOT", os.getcwd())).resolve()

_BOARD_PROPERTY = {
    "board": {
        "type": "string",
        "description": (
            "Path to a .kicad_pcb file, relative to the configured board "
            "root. Absolute paths and paths escaping the root are rejected."
        ),
    },
}


class ToolError(ValueError):
    """Tool-level input error; surfaces as isError content, not a protocol error."""


def _resolve_board(raw) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError("board must be a non-empty path string")
    try:
        resolved = confine_to_dir(BOARD_ROOT / raw, BOARD_ROOT,
                                  suffix=".kicad_pcb")
    except PathConfinementError as exc:
        raise ToolError(str(exc)) from exc
    if not resolved.is_file():
        raise ToolError(f"board file does not exist: {raw}")
    return str(resolved)


# --------------------------------------------------------------------------- #
# Tool surface
# --------------------------------------------------------------------------- #
TOOLS: dict[str, dict] = {
    "dfm_emc_check": {
        "description": (
            "Board dosyasından DFM/EMC nüans ölçümü: teardrop sayısı, "
            "via-in-pad, annular ring, EPAD pasta kapsaması, kenar/depanel "
            "seramik keepout, hızlı via'nın dönüş via'sı ve hızlı netlerin "
            "netclass'ı. Her bulguda `scanned` kapsam sayısı döner; "
            "scanned=0 -> NO_COVERAGE, sessiz PASS yoktur. Salt okunur."
        ),
        "schema": {
            **_BOARD_PROPERTY,
            "hs_net_regex": {"type": "string", "description": "Hızlı net adı regex'i."},
            "edge_keepout_mm": {"type": "number", "exclusiveMinimum": 0, "default": 2.0},
            "stitch_max_mm": {"type": "number", "exclusiveMinimum": 0, "default": 2.5},
            "paste_ratio_max": {"type": "number", "exclusiveMinimum": 0, "default": 0.80},
            "epad_area_mm2": {"type": "number", "exclusiveMinimum": 0, "default": 4.0},
            "annular_min_mm": {"type": "number", "exclusiveMinimum": 0, "default": 0.15},
        },
    },
    "mask_dam": {
        "description": (
            "Pin-arası kanalda solder-mask barajı: "
            "baraj = bakır_boşluk - expansion(a) - expansion(b); fab "
            "minimumunun altı ihlal sayılır. Salt okunur."
        ),
        "schema": {
            **_BOARD_PROPERTY,
            "min_dam_mm": {"type": "number", "exclusiveMinimum": 0, "default": 0.20},
            "search_mm": {"type": "number", "exclusiveMinimum": 0, "default": 3.0},
            "track_tol_mm": {"type": "number", "exclusiveMinimum": 0, "default": 0.05},
        },
    },
    "stitch_density": {
        "description": (
            "f_knee'den lambda/20 hedefi türetip kart kenarı ve via dönüş "
            "dikişi aralığını ölçer. er_eff kullanıcı girdisidir, "
            "otomatik hesaplanmaz. Salt okunur."
        ),
        "schema": {
            **_BOARD_PROPERTY,
            "f_knee_ghz": {"type": "number", "exclusiveMinimum": 0, "default": 5.0},
            "er": {"type": "number", "exclusiveMinimum": 0, "default": 4.5},
            "gnd_net_regex": {"type": "string"},
            "edge_step_mm": {"type": "number", "exclusiveMinimum": 0, "default": 1.0},
        },
    },
    "via_stub": {
        "description": (
            "Hızlı-sinyal via'sının kullanılmayan barel/stub boyunu ve "
            "çeyrek-dalga rezonans ön-elemesini ölçer. Kapsam yoksa PASS "
            "vermez. Sign-off 3D EM'dir. Salt okunur."
        ),
        "schema": {
            **_BOARD_PROPERTY,
            "channel_bw_ghz": {"type": "number", "exclusiveMinimum": 0},
            "data_rate_gbps": {"type": "number", "exclusiveMinimum": 0},
            "bw_margin": {"type": "number", "exclusiveMinimum": 0, "default": 2.0},
            "dk": {"type": "number", "exclusiveMinimum": 0},
            "hs_net_regex": {"type": "string"},
        },
    },
}

DEFAULTS: dict[str, dict] = {
    "dfm_emc_check": {
        "hs_net_regex": r"(?i)(usb|mipi|csi|rgmii|_d_?[pn]\b|_tx|_rx|clk)",
        "edge_keepout_mm": 2.0, "stitch_max_mm": 2.5, "paste_ratio_max": 0.80,
        "epad_area_mm2": 4.0, "annular_min_mm": 0.15,
    },
    "mask_dam": {"min_dam_mm": 0.20, "search_mm": 3.0, "track_tol_mm": 0.05},
    "stitch_density": {
        "f_knee_ghz": 5.0, "er": 4.5,
        "gnd_net_regex": r"^/?(GND|AGND|DGND|VSS)", "edge_step_mm": 1.0,
    },
    "via_stub": {
        "channel_bw_ghz": None, "data_rate_gbps": None, "bw_margin": 2.0,
        "dk": None,
        "hs_net_regex": r"(?i)(usb|mipi|csi|rgmii|_d_?[pn]\b|_tx|_rx|clk)",
    },
}


def _input_schema(name: str) -> dict:
    return {
        "type": "object",
        "properties": TOOLS[name]["schema"],
        "required": ["board"],
        "additionalProperties": False,
    }


class _Args:
    """argparse.Namespace stand-in for the checkers' ``run_analysis``."""

    def __init__(self, mapping: dict):
        for key, value in mapping.items():
            setattr(self, key, value)


def _run(name: str, args: dict) -> dict:
    import importlib

    module = importlib.import_module(name)
    import pcbnew  # noqa: F401  (fail fast with a clear message if absent)

    merged = dict(DEFAULTS[name])
    merged.update({k: v for k, v in args.items() if k != "board"})
    merged["board"] = _resolve_board(args.get("board"))
    merged["json"] = None                      # never write from the server

    if name == "via_stub":
        if merged.get("channel_bw_ghz") is not None \
                and merged.get("data_rate_gbps") is not None:
            raise ToolError(
                "channel_bw_ghz and data_rate_gbps are mutually exclusive")

    board = pcbnew.LoadBoard(merged["board"])
    return _dispatch(name, module, merged, board)


def _dispatch(name: str, module, a: dict, board) -> dict:
    """Call the checker's own check functions and build the shared envelope."""
    if name == "dfm_emc_check":
        findings = [
            module.check_teardrops(board),
            module.check_via_in_pad(board),
            module.check_annular(board, a["annular_min_mm"]),
            module.check_paste_coverage(board, a["epad_area_mm2"],
                                        a["paste_ratio_max"]),
            module.check_edge_keepout(board, a["edge_keepout_mm"]),
            module.check_return_vias(board, a["hs_net_regex"], a["stitch_max_mm"]),
            module.check_netclass_coverage(board, a["hs_net_regex"]),
        ]
        params = {k: a[k] for k in DEFAULTS[name]}
    elif name == "mask_dam":
        findings = [module.check_mask_dam(board, a["min_dam_mm"], a["search_mm"],
                                          a["track_tol_mm"])]
        params = {k: a[k] for k in DEFAULTS[name]}
    elif name == "stitch_density":
        target = module.target_spacing_mm(a["f_knee_ghz"], a["er"])
        gnd = module.gnd_vias(board, a["gnd_net_regex"])
        findings = [
            module.check_edge_stitching(board, gnd, target, a["edge_step_mm"]),
            module.check_via_return_stitching(board, gnd, target),
        ]
        params = {k: a[k] for k in DEFAULTS[name]}
        params["target_spacing_mm"] = round(target, 4)
    elif name == "via_stub":
        if a["channel_bw_ghz"] is not None:
            bw, source = a["channel_bw_ghz"], "kullanıcı verdi"
        elif a["data_rate_gbps"] is not None:
            bw = a["data_rate_gbps"] / 2.0
            source = "--data-rate-gbps'ten f_knee=data_rate/2 (NRZ Nyquist modeli)"
        else:
            bw, source = None, "verilmedi"
        stackup, status = module.parse_stackup(a["board"])
        findings = [module.check_via_stubs(board, stackup, status,
                                           a["hs_net_regex"], bw, source,
                                           a["bw_margin"], a["dk"])]
        params = {k: a[k] for k in DEFAULTS[name]}
        params["bandwidth_source"] = source
        params["channel_bw_ghz"] = bw
    else:
        raise LookupError(f"bilinmeyen tool: {name!r}")
    return board_report(name, a["board"], findings, params=params)


def _summary(name: str, report: dict) -> str:
    counts = report["summary"]
    lines = [
        f"TOOL: {name}   board={report['board']}",
        f"PASS={counts['PASS']} FAIL={counts['FAIL']} "
        f"NO_COVERAGE={counts['NO_COVERAGE']}",
    ]
    for check in report["checks"]:
        line = (f"  {check['status']:<12} {check['check']} "
                f"(scanned={check['scanned']}, "
                f"violations={len(check['violations'])})")
        lines.append(line)
    if counts["NO_COVERAGE"]:
        lines.append("NO_COVERAGE var: bu bir PASS DEĞİLDİR, kapsam eksik.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing (same shape as crosstalk_mcp_server)
# --------------------------------------------------------------------------- #
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
            "tools": [
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": _input_schema(name),
                }
                for name, spec in TOOLS.items()
            ]
        }
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise LookupError(f"bilinmeyen tool: {name!r}")
        args = params.get("arguments") or {}
        unknown = set(args) - set(TOOLS[name]["schema"])
        if unknown:
            raise ToolError(f"bilinmeyen parametre: {sorted(unknown)}")
        report = _run(name, args)
        return {
            "content": [
                {"type": "text", "text": _summary(name, report)},
                {"type": "text",
                 "text": json.dumps(report, ensure_ascii=False, indent=2,
                                    allow_nan=False)},
            ],
            "isError": False,
        }
    raise LookupError(f"desteklenmeyen metot: {method!r}")


def _write(stream, obj) -> None:
    stream.write(json.dumps(obj, ensure_ascii=False, allow_nan=False) + "\n")
    stream.flush()


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
                            "error": {"code": -32700,
                                      "message": f"parse error: {exc}"}})
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if msg_id is None:                      # notification
            continue

        try:
            result = _handle(method, params)
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})
        except ToolError as exc:
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": json.dumps(
                    {"status": "ERROR", "error": str(exc)},
                    ensure_ascii=False)}],
                "isError": True,
            }})
        except LookupError as exc:
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32601, "message": str(exc)}})
        except Exception as exc:                # noqa: BLE001 - server must survive
            _write(stdout, {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32603,
                                      "message": f"{type(exc).__name__}: {exc}"}})
        if method == "shutdown":
            break
    return 0


if __name__ == "__main__":
    sys.exit(serve())
