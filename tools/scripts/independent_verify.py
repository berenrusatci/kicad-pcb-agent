#!/usr/bin/env python3
"""Second verification layer, fully independent of KiCad DRC/ERC.

KiCad's DRC only knows generic rules (clearance, short, crossing, dangling
via). "Every segment of this net class is exactly 0.2 mm wide", "this pair's
P/N length difference stays inside the skew budget", "this net must live on
F.Cu and a segment anywhere else is a leak" - the DRC does not know any of
that, so "DRC is clean" says nothing about them. This tool reads the
``.kicad_pcb`` with its own S-expression reader (no pcbnew, and it never
touches KiCad's DRC JSON) and measures the board against a project contract.

backend: file  (pure stdlib)

CONTRACT SOURCE - CHANGED ON PORT
---------------------------------
The archived tool parsed its contract out of a project Markdown table
(``DOCS/02_Stackup_and_Impedance.md`` section 3) plus an ad-hoc
``DOCS/katman_kontrati.json``. Two problems: a renamed heading or a
reformatted table silently emptied the contract and every check quietly
degraded to NO_COVERAGE, and the contract's shape was defined only by the
regex that read it. The contract is now one JSON file whose shape is pinned
by ``tools/schemas/independent_verify_contract.schema.json``. A malformed
contract is an ERROR, not an empty PASS.

Usage:
    independent_verify.py <board.kicad_pcb> --contract contract.json
                          [--json out.json]
    independent_verify.py --self-test

Exit: 0 = every contract item was measured and clean, 1 = at least one FAIL
or NO_COVERAGE, 2 = run error. NO_COVERAGE is non-zero on purpose: a contract
item nobody could measure is not a contract item that holds.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.findings import (  # noqa: E402
    FAIL,
    NO_COVERAGE,
    Finding,
    board_report,
    result,
)
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "independent-verify-report/v1"
CONTRACT_SCHEMA_VERSION = "independent-verify-contract/v1"
TOOL_NAME = "independent_verify"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
@dataclass
class NetContract:
    """One contract row. ``None`` means the contract is silent - the
    corresponding measurement is SKIPPED, never compared against a guess."""

    name: str
    target_width_mm: float | None = None
    width_tolerance_mm: float = 0.005
    skew_tolerance_mm: float | None = None
    p_suffix: str = "_P"
    n_suffix: str = "_N"
    allowed_layers: frozenset[str] | None = None


class ContractError(ValueError):
    """The contract file is missing, malformed, or of an unknown version."""


def _required_contract_fields() -> list[str]:
    """The pinned top-level field list, read from the schema file itself."""
    schema = json.loads(
        (SCHEMA_DIR / "independent_verify_contract.schema.json")
        .read_text(encoding="utf-8"))
    return list(schema["required"])


def load_contract(path: str) -> list[NetContract]:
    """Read and validate the contract JSON.

    Deliberately strict: an unknown ``schema_version``, a missing required
    field or an empty net list raises instead of yielding an empty contract
    that would report NO_COVERAGE and look merely uncovered rather than
    misconfigured.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ContractError(f"contract not found: {path}")
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"contract is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("contract must be a JSON object")

    for field in _required_contract_fields():
        if field not in data:
            raise ContractError(f"contract is missing required field: {field}")
    if data["schema_version"] != CONTRACT_SCHEMA_VERSION:
        raise ContractError(
            f"unsupported contract schema_version "
            f"{data['schema_version']!r}; expected {CONTRACT_SCHEMA_VERSION!r}")

    default_skew = data.get("default_skew_tolerance_mm")
    nets = data["nets"]
    if not isinstance(nets, list) or not nets:
        raise ContractError("contract 'nets' must be a non-empty array")

    contracts: list[NetContract] = []
    for row in nets:
        if not isinstance(row, dict) or not row.get("name"):
            raise ContractError(f"contract net entry needs a name: {row!r}")
        layers = row.get("allowed_layers")
        contracts.append(NetContract(
            name=row["name"],
            target_width_mm=row.get("target_width_mm"),
            width_tolerance_mm=float(row.get("width_tolerance_mm", 0.005)),
            skew_tolerance_mm=row.get("skew_tolerance_mm", default_skew),
            p_suffix=row.get("p_suffix", "_P"),
            n_suffix=row.get("n_suffix", "_N"),
            allowed_layers=frozenset(layers) if layers else None,
        ))
    return contracts


# --------------------------------------------------------------------------- #
# Board reading (no pcbnew)
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    net: str
    layer: str
    width_mm: float
    x1: float
    y1: float
    x2: float
    y2: float
    kind: str   # segment | arc | via

    @property
    def length_mm(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


def _blocks(text: str, tag: str) -> list[str]:
    """``(tag ...)`` blocks, found by counting parenthesis depth."""
    out: list[str] = []
    for match in re.finditer(r"\(" + tag + r"\b", text):
        start = match.start()
        depth = 0
        i = start
        while i < len(text):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[start:i + 1])
    return out


_NET = re.compile(r'\(net "([^"]*)"\)')
_START = re.compile(r"\(start ([\d.\-]+) ([\d.\-]+)\)")
_END = re.compile(r"\(end ([\d.\-]+) ([\d.\-]+)\)")
_WIDTH = re.compile(r"\(width ([\d.]+)\)")
_LAYER = re.compile(r'\(layer "([^"]*)"\)')
_AT = re.compile(r"\(at ([\d.\-]+) ([\d.\-]+)\)")
_SIZE = re.compile(r"\(size ([\d.]+)\)")
_LAYERS = re.compile(r"\(layers ([^\)]*)\)")


def read_tracks(board_path: str) -> dict[str, list[Segment]]:
    """``segment``/``arc``/``via`` blocks grouped by net name.

    Items with no net name are skipped: they belong to no contract.
    """
    content = Path(board_path).read_text(encoding="utf-8")
    tracks: dict[str, list[Segment]] = {}

    for kind in ("segment", "arc"):
        for block in _blocks(content, kind):
            net = _NET.search(block)
            start, end = _START.search(block), _END.search(block)
            width, layer = _WIDTH.search(block), _LAYER.search(block)
            if not (net and start and end and width and layer) or not net.group(1):
                continue
            tracks.setdefault(net.group(1), []).append(Segment(
                net=net.group(1), layer=layer.group(1),
                width_mm=float(width.group(1)),
                x1=float(start.group(1)), y1=float(start.group(2)),
                x2=float(end.group(1)), y2=float(end.group(2)), kind=kind))

    for block in _blocks(content, "via"):
        net = _NET.search(block)
        at, size = _AT.search(block), _SIZE.search(block)
        if not (net and at and size) or not net.group(1):
            continue
        layers = _LAYERS.search(block)
        x, y = float(at.group(1)), float(at.group(2))
        # A via is recorded as its own kind with zero length. Changing layer is
        # what a via is FOR, so it must never count as a layer leak.
        tracks.setdefault(net.group(1), []).append(Segment(
            net=net.group(1), layer=layers.group(1) if layers else "",
            width_mm=float(size.group(1)), x1=x, y1=y, x2=x, y2=y, kind="via"))

    return tracks


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #
def check_width(tracks, pattern: str, expected_mm: float,
                tolerance_mm: float = 0.005) -> Finding:
    """Segment-width spread over every net matching ``pattern`` (vias excluded).

    One fixed value is expected; a mix (part drawn at 0.2, part at 0.25) is a
    FAIL even though every individual value may look plausible.
    """
    regex = re.compile(pattern)
    matched = [s for net, segs in tracks.items() if regex.search(net)
               for s in segs if s.kind == "segment"]
    violations = [
        {"net": s.net, "layer": s.layer, "measured_mm": s.width_mm,
         "expected_mm": expected_mm,
         "delta_mm": round(abs(s.width_mm - expected_mm), 4)}
        for s in matched if abs(s.width_mm - expected_mm) > tolerance_mm
    ]
    return result(f"track_width[{pattern}]", len(matched), violations,
                  f"expected={expected_mm}mm tolerance=+/-{tolerance_mm}mm")


def _net_length(segments) -> float:
    return sum(s.length_mm for s in segments if s.kind in ("segment", "arc"))


def check_skew(tracks, pair_name: str, p_suffix: str = "_P",
               n_suffix: str = "_N", tolerance_mm: float = 15.0) -> Finding:
    """P/N total routed length difference.

    If either net is absent from the board (not routed yet) the result is
    NO_COVERAGE: "skew is 0" would be a lie of omission, not a pass.
    """
    p_net, n_net = f"{pair_name}{p_suffix}", f"{pair_name}{n_suffix}"
    p_segments, n_segments = tracks.get(p_net, []), tracks.get(n_net, [])
    if not p_segments or not n_segments:
        return result(f"skew[{pair_name}]", 0, [],
                      f"{p_net}/{n_net} not routed")
    length_p, length_n = _net_length(p_segments), _net_length(n_segments)
    skew = abs(length_p - length_n)
    violations = []
    if skew > tolerance_mm:
        violations.append({
            "pair": pair_name,
            "length_p_mm": round(length_p, 3),
            "length_n_mm": round(length_n, 3),
            "skew_mm": round(skew, 3),
            "tolerance_mm": tolerance_mm,
        })
    return result(f"skew[{pair_name}]", 1, violations,
                  f"tolerance={tolerance_mm}mm")


def check_layer_leak(tracks, pattern: str, allowed: frozenset) -> Finding:
    """Segments/arcs of matching nets on a layer the contract does not allow.

    Vias are excluded on purpose: see ``read_tracks``.
    """
    regex = re.compile(pattern)
    matched = [s for net, segs in tracks.items() if regex.search(net)
               for s in segs if s.kind in ("segment", "arc")]
    violations = [{"net": s.net, "layer": s.layer, "kind": s.kind}
                  for s in matched if s.layer not in allowed]
    return result(f"layer_leak[{pattern}]", len(matched), violations,
                  f"allowed={sorted(allowed)}")


def verify(board_path: str, contracts) -> list[Finding]:
    """Run every contract item that has something to measure."""
    tracks = read_tracks(board_path)
    findings: list[Finding] = []
    for contract in contracts:
        pattern = re.escape(contract.name)
        if contract.target_width_mm is not None:
            findings.append(check_width(tracks, pattern, contract.target_width_mm,
                                        contract.width_tolerance_mm))
        if contract.skew_tolerance_mm is not None:
            findings.append(check_skew(tracks, contract.name, contract.p_suffix,
                                       contract.n_suffix,
                                       contract.skew_tolerance_mm))
        if contract.allowed_layers is not None:
            findings.append(check_layer_leak(tracks, pattern,
                                             contract.allowed_layers))
    if not findings:
        findings.append(result(
            "project_contract", 0, [],
            "the contract lists nets but none of them state a width, a skew "
            "tolerance or an allowed layer set - nothing was measurable"))
    return findings


# --------------------------------------------------------------------------- #
_SAMPLE_BOARD = """(kicad_pcb
  (segment (start 0 0) (end 10 0) (width 0.2) (layer "F.Cu") (net "USB_D_P"))
  (segment (start 0 1) (end 10 1) (width 0.2) (layer "F.Cu") (net "USB_D_N"))
  (segment (start 10 1) (end 14 1) (width 0.2) (layer "F.Cu") (net "USB_D_N"))
  (via (at 5 5) (size 0.6) (layers "F.Cu" "B.Cu") (net "USB_D_P"))
)
"""


def self_test() -> list[str]:
    import tempfile

    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        board = Path(tmp) / "t.kicad_pcb"
        board.write_text(_SAMPLE_BOARD, encoding="utf-8")
        tracks = read_tracks(str(board))

        if set(tracks) != {"USB_D_P", "USB_D_N"}:
            problems.append(f"net grouping wrong: {sorted(tracks)}")
        if len(tracks["USB_D_P"]) != 2:
            problems.append("the via was not attached to its net")

        clean = check_width(tracks, "USB_D", 0.2)
        if clean["status"] != "PASS":
            problems.append(f"uniform 0.2 mm width did not PASS: {clean}")

        # FAULT INJECTION: demanding 0.25 mm must fail on every segment.
        broken = check_width(tracks, "USB_D", 0.25)
        if broken["status"] != FAIL or len(broken["violations"]) != 3:
            problems.append("fault injection did not hold: width never compared")

        # Skew: P is 10 mm, N is 14 mm -> 4 mm.
        tight = check_skew(tracks, "USB_D", tolerance_mm=1.0)
        if tight["status"] != FAIL:
            problems.append("a 4 mm skew passed a 1 mm tolerance")
        loose = check_skew(tracks, "USB_D", tolerance_mm=10.0)
        if loose["status"] != "PASS":
            problems.append("a 4 mm skew failed a 10 mm tolerance")

        # An unrouted pair is NO_COVERAGE, not a pass.
        if check_skew(tracks, "NOWHERE")["status"] != "NO_COVERAGE":
            problems.append("an unrouted pair did not report NO_COVERAGE")

        # The via must not count as a layer leak.
        leak = check_layer_leak(tracks, "USB_D", frozenset({"F.Cu"}))
        if leak["status"] != "PASS":
            problems.append(f"the via was mistaken for a layer leak: {leak}")
        real_leak = check_layer_leak(tracks, "USB_D", frozenset({"B.Cu"}))
        if real_leak["status"] != FAIL:
            problems.append("a real layer leak was not detected")

    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("board", nargs="?", help="path to the .kicad_pcb")
    parser.add_argument("--contract",
                        help="contract JSON "
                             "(schemas/independent_verify_contract.schema.json)")
    parser.add_argument("--json", help="also write the JSON report here")
    parser.add_argument("--self-test", action="store_true",
                        help="run the built-in self-test and exit (opt-in)")
    args = parser.parse_args(argv)

    if args.self_test:
        problems = self_test()
        for problem in problems:
            diag(f"SELF-TEST FAIL: {problem}")
        if problems:
            return 1
        diag("SELF-TEST PASS: independent_verify")
        return 0

    if not args.board:
        parser.error("the board path is required")
    if not args.contract:
        parser.error("--contract is required: the project contract is never "
                     "inferred from the board it is meant to check")

    contracts = load_contract(args.contract)
    findings = verify(args.board, contracts)
    report = board_report(TOOL_NAME, args.board, findings, params={
        "contract": args.contract,
        "contract_schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_nets": [c.name for c in contracts],
    }, schema_version=SCHEMA_VERSION)
    write_report(report, args.json)
    emit(report)
    # An unmeasured contract item must not leave through exit 0. This
    # expression used to branch on FAIL alone - a hand-rolled copy of
    # common.findings.board_exit_code that never inherited that function's
    # documented (and deliberately frozen) NO_COVERAGE exemption. verify()
    # returns NO_COVERAGE for a net the board never routed and for a contract
    # that states nothing measurable at all, and a chain reading exit 0 would
    # take either for "the project contract holds".
    summary = report["summary"]
    return 1 if summary[FAIL] or summary[NO_COVERAGE] else 0


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
