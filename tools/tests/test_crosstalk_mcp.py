#!/usr/bin/env python3
"""crosstalk_mcp_server.py protokol + sözleşme regresyonu (stdlib unittest)."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import SCRIPTS, SERVERS  # noqa: E402

import crosstalk_mcp_server as srv  # noqa: E402
import crosstalk_thinning as eng  # noqa: E402


def roundtrip(*messages) -> list[dict]:
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = io.StringIO()
    srv.serve(stdin, stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


def call(**args) -> dict:
    out = roundtrip({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": srv.TOOL_NAME, "arguments": args}})
    return out[0]


def payload(resp: dict) -> dict:
    return json.loads(resp["result"]["content"][1]["text"])


class Protocol(unittest.TestCase):
    def test_initialize(self):
        r = roundtrip({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": srv.PROTOCOL_VERSION}})[0]
        self.assertEqual(r["result"]["protocolVersion"], srv.PROTOCOL_VERSION)
        self.assertEqual(r["result"]["serverInfo"]["name"], srv.SERVER_NAME)

    def test_tools_list_schema(self):
        r = roundtrip({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0]
        tools = r["result"]["tools"]
        self.assertEqual(len(tools), 1)
        t = tools[0]
        self.assertEqual(t["name"], "analyze_crosstalk_and_thinning")
        self.assertEqual(t["inputSchema"]["required"], ["trace_width_mm", "spacing_mm"])
        # şema anahtarları motorun imzasıyla birebir örtüşmeli
        import inspect
        sig = set(inspect.signature(eng.analyze).parameters)
        self.assertEqual(set(t["inputSchema"]["properties"]), sig)

    def test_notification_no_response(self):
        out = roundtrip({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(out, [])

    def test_unknown_method(self):
        r = roundtrip({"jsonrpc": "2.0", "id": 3, "method": "yok"})[0]
        self.assertEqual(r["error"]["code"], -32601)

    def test_bad_json_does_not_kill_server(self):
        stdin = io.StringIO('{bozuk\n' + json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
        stdout = io.StringIO()
        srv.serve(stdin, stdout)
        out = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(out[0]["error"]["code"], -32700)
        self.assertEqual(out[1]["id"], 9)


class ToolContract(unittest.TestCase):
    def test_crosstalk_risk_never_thins(self):
        p = payload(call(trace_width_mm=0.20, spacing_mm=0.10,
                         dielectric_height_mm=0.20, coupled_length_mm=60,
                         rise_time_ps=200))
        self.assertEqual(p["status"], "CROSSTALK_RISK")
        self.assertEqual(p["suggested_width_mm"], 0.20)
        self.assertGreater(p["suggested_spacing_mm"], 0.10)

    def test_safe_to_thin_respects_fab_floor(self):
        p = payload(call(trace_width_mm=0.40, spacing_mm=1.60,
                         dielectric_height_mm=0.10, coupled_length_mm=20,
                         rise_time_ps=500, fab_min_width_mm=0.25))
        self.assertEqual(p["status"], "SAFE_TO_THIN")
        self.assertGreaterEqual(p["suggested_width_mm"], 0.25)

    def test_missing_em_inputs_is_no_coverage(self):
        p = payload(call(trace_width_mm=0.20, spacing_mm=0.60))
        self.assertEqual(p["status"], "NO_COVERAGE")
        self.assertEqual(p["coverage"]["scanned"], 0)

    def test_impedance_controlled_blocked(self):
        p = payload(call(trace_width_mm=0.40, spacing_mm=1.60,
                         dielectric_height_mm=0.10, coupled_length_mm=20,
                         rise_time_ps=500, impedance_controlled=True))
        self.assertEqual(p["status"], "THINNING_BLOCKED")
        self.assertEqual(p["suggested_width_mm"], 0.40)

    def test_never_signoff(self):
        p = payload(call(trace_width_mm=0.40, spacing_mm=1.60,
                         dielectric_height_mm=0.10, coupled_length_mm=20,
                         rise_time_ps=500))
        self.assertIs(p["signoff"], False)
        self.assertEqual(p["model_class"], "exploratory_closed_form")

    def test_invalid_input_is_tool_error_not_crash(self):
        r = call(trace_width_mm=-1, spacing_mm=0.2)
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["status"], "ERROR")

    def test_unknown_argument_rejected(self):
        r = call(trace_width_mm=0.2, spacing_mm=0.2, uydurma_param=1)
        self.assertTrue(r["result"]["isError"])

    def test_unknown_tool_name(self):
        out = roundtrip({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "baska_tool", "arguments": {}}})[0]
        self.assertEqual(out["error"]["code"], -32601)

    def test_summary_carries_action(self):
        r = call(trace_width_mm=0.20, spacing_mm=0.10, dielectric_height_mm=0.20,
                 coupled_length_mm=60, rise_time_ps=200)
        text = r["result"]["content"][0]["text"]
        self.assertIn("STATUS: CROSSTALK_RISK", text)
        self.assertIn("suggested_spacing_mm", text)
        self.assertIn("signoff=False", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
