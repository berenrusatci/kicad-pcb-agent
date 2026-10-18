# kicad-pcb-agent

Skills and verification tools for an LLM-driven PCB design agent working on
KiCad 10 projects. The agent proposes; the tools measure. Every gate reports
what it actually scanned, so "nothing checked" can never pass as "all clear".

> Belgelerin çoğu Türkçe. / Most of the in-repo documentation is in Turkish.

## Contents

```
skills/   24 agent skills (SKILL.md contracts): orchestration, schematic/ERC,
          stackup & impedance, high-speed routing, DFM, EMC/ESD, power/thermal,
          BOM lifecycle, test points, bring-up, design checking …
tools/    the code the skills call
  common/    shared modules (S-expression parser, units, hashing, findings …)
  scripts/   stdlib-only validators and solvers; board/ needs pcbnew
  servers/   MCP servers (board checks, crosstalk)
  schemas/   JSON Schemas for every report
  tests/     unittest suite
  fixtures/  test data
config/   tool_registry.json: hash-bound registry of every tool
docs/     tool inventory and consolidation notes
```

Start with [`skills/README.md`](skills/README.md) and
[`tools/README.md`](tools/README.md).

## Requirements

- Python ≥ 3.11. The core has **no runtime dependencies**.
- Optional: KiCad 10 (`kicad-cli`, `pcbnew`), ngspice, Freerouting, KiBot.
  Tests that need them are skipped when they are missing.

## Running the tests

```sh
./tools/run_tests.sh          # everything
./tools/run_tests.sh -v       # verbose
python3 tools/scripts/sync_tool_registry.py --check   # registry drift check
```

## Tool contract

Every tool follows the same rules (enforced by `tools/common/report_io.py`):
required inputs are positional, `--json` always writes, stdout carries only
machine-readable JSON, diagnostics go to stderr, exit codes are
`0 = ok · 1 = fail · 2 = error`.
