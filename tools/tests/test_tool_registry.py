#!/usr/bin/env python3
"""Guard config/tool_registry.json against drift in BOTH directions.

This is the cheapest test in the bundle and it is here because the registry
had silently rotted: every ``local`` locator still carried an ``aiplanlama/``
prefix from a pre-split repo layout (so every local tool resolved to nothing),
and nine scripts on disk were missing from the registry entirely, which meant
no context pack could legally authorise a board-geometry check.

Direction A: every registry entry points at a file that exists and hashes.
Direction B: every script with a __main__ entrypoint appears in the registry.
Direction C: every tool a skill names actually exists in the registry.
Direction D: every registered tool is named by at least one skill, or is
             exempt for a stated reason. D was the missing one: on 2026-08-09
             only 30 of 82 tools appeared in any skill's ``tools:`` list, so
             52 tested, hash-bound tools could be reached only if a human
             picked them by hand.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO, SCRIPTS, SERVERS, TOOL_REGISTRY, TOOLS  # noqa: E402

from common.hashing import sha256_file, valid_digest  # noqa: E402

SYNC = SCRIPTS / "sync_tool_registry.py"


class ToolRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(TOOL_REGISTRY.read_text(encoding="utf-8"))
        cls.tools = cls.registry["tools"]
        cls.by_name = {t["name"]: t for t in cls.tools}

    # ------------------------------------------------------------- structure
    def test_schema_version_is_pinned(self):
        self.assertEqual("pcb-tool-registry/v1",
                         self.registry["schema_version"])

    def test_tool_names_are_unique(self):
        names = [t["name"] for t in self.tools]
        self.assertEqual(len(names), len(set(names)),
                         "duplicate tool name in registry")

    def test_every_entry_has_the_required_fields(self):
        for tool in self.tools:
            with self.subTest(tool=tool.get("name")):
                for field in ("name", "kind", "locator", "version", "modes",
                              "available", "preconditions"):
                    self.assertIn(field, tool)
                self.assertIn(tool["kind"],
                              {"builtin", "local", "system", "mcp", "human"})
                self.assertIsInstance(tool["modes"], list)
                self.assertTrue(tool["modes"])
                self.assertIsInstance(tool["available"], bool)

    # --------------------------------------------- direction A: registry -> disk
    def test_every_local_locator_resolves_under_the_repo(self):
        """The exact failure that was live: locators pointing at nothing."""
        for tool in self.tools:
            if tool["kind"] != "local":
                continue
            with self.subTest(tool=tool["name"]):
                path = REPO / tool["locator"]
                self.assertFalse(Path(tool["locator"]).is_absolute(),
                                 "locator must be repo-relative")
                self.assertNotIn("..", Path(tool["locator"]).parts)
                self.assertTrue(path.is_file(),
                                f"MISSING_REGISTERED_TOOL: {tool['locator']}")

    def test_every_local_sha256_matches_the_file(self):
        for tool in self.tools:
            if tool["kind"] != "local":
                continue
            with self.subTest(tool=tool["name"]):
                self.assertTrue(valid_digest(tool.get("sha256")),
                                "sha256 must be 64 hex chars")
                actual = sha256_file(REPO / tool["locator"])
                self.assertEqual(
                    actual, tool["sha256"],
                    f"STALE_REGISTERED_TOOL: {tool['name']} "
                    f"(run sync_tool_registry.py --write)")

    def test_mcp_server_hashes_match(self):
        servers = {
            "crosstalk-mcp": SERVERS / "crosstalk_mcp_server.py",
            "board-checks-mcp": SERVERS / "board_mcp_server.py",
        }
        for name, path in servers.items():
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name)
                self.assertTrue(path.is_file())
                self.assertEqual(sha256_file(path),
                                 self.by_name[name]["sha256"])

    # --------------------------------------------- direction B: disk -> registry
    def _entrypoint_scripts(self) -> list[Path]:
        found = []
        for path in sorted(SCRIPTS.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if '__main__' in path.read_text(encoding="utf-8"):
                found.append(path)
        return found

    def test_every_entrypoint_script_is_registered(self):
        """The other direction: scripts that exist but nothing may authorise."""
        registered = {
            (REPO / t["locator"]).resolve()
            for t in self.tools if t["kind"] == "local"
        }
        missing = [
            str(p.relative_to(REPO))
            for p in self._entrypoint_scripts()
            if p.resolve() not in registered
        ]
        self.assertEqual([], missing,
                         "scripts on disk but absent from the registry")

    def test_library_only_modules_are_registered_too(self):
        """sch_wire/sch_route have no CLI but are still tools a pack may cite."""
        for name in ("sch-wire", "sch-route"):
            self.assertIn(name, self.by_name)

    def test_board_checkers_are_all_registered(self):
        for name in ("dfm-emc-check", "mask-dam", "stitch-density", "via-stub"):
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name)
                self.assertEqual(["read"], self.by_name[name]["modes"],
                                 "board checkers must be read-only")

    # ------------------------------------------------------------- generator
    def test_sync_tool_registry_reports_no_drift(self):
        proc = subprocess.run(
            [sys.executable, str(SYNC), "--check"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
            check=False,
        )
        report = json.loads(proc.stdout)
        self.assertEqual([], report["problems"], proc.stdout)
        self.assertEqual(0, proc.returncode)

    # ------------------------------------------------------- skill <-> registry
    def test_every_skill_tool_reference_resolves(self):
        """Direction C: a skill's ``tools:`` front matter must name tools that
        EXIST.

        MEASURED before this test was written: 15 of 18 skills named at least
        one tool the registry does not have. Two failure modes, and the second
        is the dangerous one:

          * spelling drift - the registry is hyphenated (``dfm-emc-check``),
            the skills were written with the module's underscore name;
          * archive names that were never ported - ``bom_lifecycle_koprusu``,
            ``uretim_ciktilari_cli``, ``mcad_carpisma_koprusu``, ``drc.sh``.

        Either way an agent reading the skill looks for a tool it cannot
        find, and the skill's promise is not backed by anything. That is
        exactly the claim-vs-reality gap this bundle exists to close, so it
        gets a test rather than a one-time fix.
        """
        import re
        skills = sorted((REPO / "skills").glob("*/SKILL.md"))
        self.assertTrue(skills, "no skills found - the glob is wrong, not the skills")
        for skill in skills:
            text = skill.read_text(encoding="utf-8")
            match = re.search(r"^tools:\s*\[(.*?)\]", text, re.M | re.S)
            if not match:
                continue
            for ref in [x.strip() for x in match.group(1).split(",") if x.strip()]:
                with self.subTest(skill=skill.parent.name, tool=ref):
                    self.assertIn(ref, self.by_name,
                                  f"{skill.parent.name} names a tool that is not in the registry")

    # Tools that legitimately belong to NO skill. Writing them into one would
    # be inventing an owner to make a counter go green - the same vacuous
    # green this bundle keeps finding elsewhere. Each needs a reason, and the
    # reason is checked below for being non-empty.
    SKILL_EXEMPT = {
        "exec": "harness builtin (exec_command); not a repo tool",
        "web": "harness builtin (web.run); not a repo tool",
        "ngspice": "external binary; the repo-side wrapper is ngspice-bridge "
                   "and that one IS cited by guc-isil",
        "kicad-ipc": "MCP server; skills reach KiCad through the kicad-cli / "
                     "pcbnew flow rather than by this tool name",
        "board-checks-mcp": "MCP server; the checkers it wraps "
                            "(dfm-emc-check, mask-dam, stitch-density, "
                            "via-stub) are each cited on their own",
    }

    def _skill_tool_citations(self) -> dict[str, list[str]]:
        import re
        citations: dict[str, list[str]] = {}
        for skill in sorted((REPO / "skills").glob("*/SKILL.md")):
            text = skill.read_text(encoding="utf-8")
            match = re.search(r"^tools:\s*\[(.*?)\]", text, re.M | re.S)
            if not match:
                continue
            for ref in [x.strip() for x in match.group(1).split(",") if x.strip()]:
                citations.setdefault(ref, []).append(skill.parent.name)
        return citations

    def test_every_registered_tool_is_reachable_from_some_skill(self):
        """Direction D: a tool no skill names is a tool no agent will trigger.

        MEASURED before this test was written: 82 registered tools, 30 named
        by a skill, 52 orphaned. Those 52 were not broken - they were tested,
        hash-bound and working. They were simply invisible to the thing meant
        to call them, which is a different failure and a quieter one: nothing
        goes red, the capability just never fires.

        Reachability is not the same as guidance. This test proves an agent
        CAN find the tool; whether it knows WHEN to call it lives in the
        skill body, and the hook lines added alongside this test are that
        half. A name in a list with no body text is the vacuous version.
        """
        cited = self._skill_tool_citations()
        orphans = sorted(
            name for name in self.by_name
            if name not in cited and name not in self.SKILL_EXEMPT)
        self.assertEqual(
            [], orphans,
            "registered but named by no skill: an agent can only reach these "
            "by hand. Wire each into the skill that owns it, or add it to "
            "SKILL_EXEMPT with the reason it has no owner.")

    def test_the_exemption_list_does_not_rot(self):
        """An exemption for a tool that IS cited is a stale excuse, and a
        stale excuse is how a list stops describing the tree."""
        cited = self._skill_tool_citations()
        for name, reason in sorted(self.SKILL_EXEMPT.items()):
            with self.subTest(tool=name):
                self.assertIn(name, self.by_name,
                              "exempting a tool that is not in the registry")
                self.assertNotIn(name, cited,
                                 "exempt but a skill names it anyway; drop "
                                 "the exemption rather than keeping both")
                self.assertTrue(reason.strip(), "exemption without a reason")

    def test_mutating_tools_are_marked_write(self):
        for name in ("rewire", "wireify"):
            with self.subTest(tool=name):
                self.assertIn("write", self.by_name[name]["modes"])
                joined = " ".join(self.by_name[name]["preconditions"])
                self.assertIn("MCP", joined,
                              "a mutating tool must say it is not MCP-exposed")


class GeneratedFilesAreNotLineMergedTests(unittest.TestCase):
    """The registry is GENERATED, and git line-merged it into a third version.

    Two branches each added tools. git merged config/tool_registry.json line
    by line, produced a file matching NEITHER side, and reported no conflict.
    It was still valid JSON, so nothing downstream noticed until
    sync_tool_registry.py --check ran and reported LOCATOR_DRIFT plus a stale
    fixture hash.

    A generated file has no meaningful line-level merge: the right answer is
    always "regenerate from the sources, which merged normally". The
    .gitattributes rule makes git refuse instead of inventing, and these tests
    pin it so a future .gitattributes edit cannot quietly drop it.
    """

    GENERATED = (
        "pcb-agent/config/tool_registry.json",
        "pcb-agent/docs/YETENEK_ENVANTERI.csv",
    )

    def setUp(self):
        self.attributes = REPO.parent / ".gitattributes"
        if not self.attributes.is_file():
            self.skipTest(".gitattributes is not present in this checkout")
        self.text = self.attributes.read_text(encoding="utf-8")

    def test_every_generated_file_is_declared_unmergeable(self):
        for path in self.GENERATED:
            with self.subTest(path=path):
                self.assertIn(f"{path} -merge", self.text,
                              f"{path} is generated and must not be line-merged")

    def test_the_rule_uses_minus_merge_not_merge_ours(self):
        """MEASURED: merge=ours does nothing without a per-clone
        `git config merge.ours.driver true`, which cannot be committed and is
        therefore forgotten - it then silently falls back to a line merge,
        which is the exact failure this rule exists to prevent."""
        for path in self.GENERATED:
            with self.subTest(path=path):
                self.assertNotIn(f"{path} merge=ours", self.text)

    def test_the_mixed_fixture_is_deliberately_left_line_merged(self):
        """context_manifest.json is hand-authored structure plus generated
        hashes. '-merge' would make an all-or-nothing choice and drop the
        other side's real edits, so it is excluded on purpose."""
        self.assertNotIn("context_manifest.json -merge", self.text)

    def test_git_actually_applies_the_rule(self):
        """The file can say anything; this asks git what it will do."""
        try:
            proc = subprocess.run(
                ["git", "check-attr", "merge", "--", *self.GENERATED],
                cwd=str(REPO.parent), capture_output=True, text=True,
                timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
            self.skipTest(f"git is not usable here: {exc}")
        if proc.returncode != 0:  # pragma: no cover - not a git checkout
            self.skipTest("not a git checkout")
        for line in proc.stdout.splitlines():
            with self.subTest(line=line):
                self.assertTrue(line.strip().endswith("merge: unset"), line)

    def test_the_regeneration_command_is_documented_beside_the_rule(self):
        """A rule that turns a silent mis-merge into a conflict is only half
        the fix; the reader has to be told the resolution is to regenerate."""
        self.assertIn("sync_tool_registry.py --write", self.text)


if __name__ == "__main__":
    unittest.main()
