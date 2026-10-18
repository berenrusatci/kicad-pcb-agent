"""Contract tests for the GPTPro handoff export.

The export decides what an external reviewer is even able to see, so its
failures are silent by construction: a bundle that quietly dropped a required
source looks exactly like a bundle whose source was empty. Every test here
injects a specific fault and asserts the export refuses rather than warns.

The suite builds a small fixture repo per test — no network, no KiCad, no
dependency on the real tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import REPO  # noqa: E402

GPTPRO = REPO / "skills" / "gptpro"
EXPORT_SH = GPTPRO / "scripts" / "export.sh"
ENGINE = GPTPRO / "scripts" / "gptpro_export.py"


class ExportHarness(unittest.TestCase):
    """A throwaway repo plus a runner. Subclasses shape the fixture."""

    maxDiff = None

    def setUp(self) -> None:
        if not EXPORT_SH.is_file():
            self.skipTest("skills/gptpro is not in this checkout")
        self.tmp = Path(tempfile.mkdtemp(prefix="gptpro-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        self.out = self.tmp / "out"
        self.root.mkdir()
        self.out.mkdir()
        self.write("README.md", "# fixture\n")
        self.write("src/app.py", "print('hello')\n")
        self.write("src/util.py", "X = 1\n")
        self.write("src/dist/bundle.js", "// generated\n")
        self.write("board/build/erc.json", '{"violations": 0}\n')
        self.write("board/build/netlist.net", "(export (nets))\n")
        self.write("board/build/fault/summary.json", '{"case": "open"}\n')
        self.write("board/build/fault/big.txt", "x" * 512)
        self.write("board/design.kicad_sch", "(kicad_sch)\n")
        self.write("docs/notes.md", "notes\n")

    # -- fixture helpers ---------------------------------------------------

    def write(self, rel: str, content: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_conf(self, body: str) -> Path:
        conf = self.tmp / "bundles.conf"
        conf.write_text(body, encoding="utf-8")
        return conf

    def default_conf(self) -> Path:
        return self.write_conf(
            "EXCLUDES+=( --exclude '*.pdf' )\n"
            'define_bundle code "application source" dir:src file:README.md\n'
            'define_bundle evidence "board verification corpus" \\\n'
            "  dir:board/build:fault \\\n"
            "  file:board/build/fault/summary.json:board/fault\n"
        )

    def run_export(self, conf: Path, **env_extra) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({
            "PROJECT_ROOT": str(self.root),
            "OUTPUT_DIR": str(self.out),
            "BUNDLES_CONF": str(conf),
            "BUNDLE_PREFIX": "fixture",
        })
        env.update({k: str(v) for k, v in env_extra.items()})
        return subprocess.run(
            ["bash", str(EXPORT_SH)],
            capture_output=True, text=True, env=env, timeout=300,
        )

    # -- accessors ---------------------------------------------------------

    @property
    def current(self) -> Path:
        return self.out / "current"

    def receipt(self) -> dict:
        return json.loads((self.current / "handoff_receipt.json").read_text("utf-8"))

    def manifest(self, bundle: str) -> dict:
        zip_path = self.current / f"fixture-{bundle}.zip"
        with zipfile.ZipFile(zip_path) as zf:
            return json.loads(zf.read(f"{bundle}/MANIFEST.json"))


class HappyPath(ExportHarness):

    def setUp(self) -> None:
        super().setUp()
        proc = self.run_export(self.default_conf())
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)

    def test_publishes_a_complete_set_behind_current(self):
        self.assertTrue(self.current.is_symlink())
        names = sorted(p.name for p in self.current.iterdir())
        self.assertIn("handoff_receipt.json", names)
        self.assertIn("00-OKUNACAK.md", names)
        self.assertIn("fixture-code.zip", names)
        self.assertIn("fixture-evidence.zip", names)

    def test_coverage_arithmetic_closes_on_both_axes(self):
        for bundle in ("code", "evidence"):
            m = self.manifest(bundle)
            for axis in ("coverage_files", "coverage_bytes"):
                c = m[axis]
                self.assertEqual(
                    c["scanned"] + c["excluded"], c["eligible"],
                    f"{bundle}.{axis} does not close: {c}",
                )
                self.assertLessEqual(c["scanned"], c["eligible"])
                self.assertLessEqual(c["violations"], c["scanned"])

    def test_scanned_is_never_zero_in_a_published_set(self):
        for b in self.receipt()["bundles"]:
            self.assertGreater(b["files"], 0, f"{b['name']} published with scanned=0")

    def test_exclusion_inventory_attributes_every_pattern_to_a_policy_source(self):
        m = self.manifest("code")
        by_pattern = {e["pattern"]: e for e in m["exclusions"]}
        self.assertIn("dist", by_pattern)
        generated = by_pattern["dist"]
        self.assertEqual(generated["policy_source"], "tool_global")
        self.assertEqual(generated["count"], 1)
        self.assertGreater(generated["bytes"], 0)
        self.assertTrue(generated["reason"])
        for entry in m["exclusions"]:
            self.assertIn(entry["policy_source"],
                          ("tool_global", "project_config", "source_decl"))

    def test_source_local_exclusion_is_attributed_to_the_declaration(self):
        m = self.manifest("evidence")
        fault = [e for e in m["exclusions"] if e["pattern"] == "fault"]
        self.assertEqual(len(fault), 1)
        self.assertEqual(fault[0]["policy_source"], "source_decl")

    def test_a_build_root_named_as_a_source_defeats_the_global_build_exclude(self):
        """`build` is a global exclude, yet naming it as the source root stages
        its contents. Without this the hardware repo has to list evidence files
        one by one, which is how PROC-R went missing from `verify` for months."""
        m = self.manifest("evidence")
        staged = {f["repo_path"] for f in m["files"]}
        self.assertIn("board/build/erc.json", staged)
        self.assertIn("board/build/netlist.net", staged)

    def test_every_staged_file_carries_a_sha256_that_matches_the_archive(self):
        for bundle in ("code", "evidence"):
            m = self.manifest(bundle)
            self.assertTrue(m["files"])
            with zipfile.ZipFile(self.current / f"fixture-{bundle}.zip") as zf:
                for f in m["files"]:
                    import hashlib
                    blob = zf.read(f"{bundle}/{f['path']}")
                    self.assertEqual(hashlib.sha256(blob).hexdigest(), f["sha256"],
                                     f"{bundle}/{f['path']}")
                    self.assertEqual(len(blob), f["size_bytes"])

    def test_manifest_records_repo_path_separately_from_bundle_path(self):
        """A `file:` source may be re-homed by its :subdir. Losing the original
        repo path would make a cited finding un-anchorable in the live tree."""
        m = self.manifest("evidence")
        entry = next(f for f in m["files"] if f["path"] == "board/fault/summary.json")
        self.assertEqual(entry["repo_path"], "board/build/fault/summary.json")

    def test_snapshot_identity_is_present(self):
        r = self.receipt()
        self.assertTrue(r["snapshot_id"])
        self.assertTrue(r["export_started_utc"].endswith("Z"))
        self.assertTrue(r["export_finished_utc"].endswith("Z"))
        self.assertEqual(len(r["export_tool_sha256"]), 64)
        self.assertEqual(len(r["bundles_conf_sha256"]), 64)
        for b in r["bundles"]:
            self.assertEqual(len(b["zip_sha256"]), 64)
            self.assertEqual(len(b["manifest_sha256"]), 64)

    def test_guide_bundle_list_equals_receipt_bundle_list(self):
        """J-4 regression fixture.

        The hand-maintained 00-OKUNACAK.md described seven zips next to a set of
        eight. Fixing that file by hand fixes one instance of the bug; this
        assertion fixes the class.
        """
        guide = (self.current / "00-OKUNACAK.md").read_text("utf-8")
        receipt = self.receipt()
        zip_names = [b["zip_name"] for b in receipt["bundles"]]
        in_guide = sorted(
            name for name in zip_names if f"`{name}`" in guide
        )
        self.assertEqual(in_guide, sorted(zip_names),
                         "generated guide does not list exactly the receipt's bundles")
        published = sorted(p.name for p in self.current.glob("*.zip"))
        self.assertEqual(published, sorted(zip_names))

    def test_credential_scan_reports_under_its_own_scope(self):
        cs = self.receipt()["credential_scan"]
        self.assertEqual(cs["scope"], "credential_scan")
        self.assertIn("credential", cs["asserts"])
        self.assertIn("disclosure", cs["does_not_assert"])
        guide = (self.current / "00-OKUNACAK.md").read_text("utf-8")
        self.assertNotIn("safe to upload", guide.lower())

    def test_disclosure_unknown_is_confirm_and_blocks_upload(self):
        r = self.receipt()
        self.assertEqual(r["disclosure_gate"]["status"], "CONFIRM")
        self.assertFalse(r["disclosure_gate"]["upload_authorized"])
        self.assertFalse(r["upload_authorized"])
        self.assertEqual(r["verdict"], "CONFIRM")

    def test_verdict_comes_from_the_closed_vocabulary(self):
        closed = {
            "GREEN", "RED", "CONFIRM", "SCREEN_ONLY", "LOCAL_POLICY", "SIM_ONLY",
            "NO_COVERAGE", "INVALID_METRIC", "INVALID_COVERAGE", "INVALID_SPARAM",
            "INCONCLUSIVE", "STALE", "GEOMETRY_PRECHECK_PASS", "TBD", "API_YOK",
        }
        r = self.receipt()
        self.assertIn(r["verdict"], closed)
        self.assertIn(r["disclosure_gate"]["status"], closed)

    def test_receipt_states_what_it_does_not_assert(self):
        r = self.receipt()
        self.assertEqual(
            r["authority"]["asserts"],
            "this snapshot carries the declared scope at these hashes",
        )
        self.assertTrue(any("PCB" in s for s in r["authority"]["does_not_assert"]))
        self.assertIn("NOT a statement about the PCB design", r["verdict_scope"])

    def test_manifest_lists_siblings_so_absence_is_readable(self):
        m = self.manifest("code")
        self.assertEqual([s["name"] for s in m["sibling_bundles"]], ["evidence"])
        self.assertNotIn("MANIFEST.json", {f["path"] for f in m["files"]})
        self.assertTrue(m["blind_spot_note"])

    def test_evidence_index_is_hash_bound_and_screen_only(self):
        """A derived view is a new surface to be wrong on, so it is only allowed
        under three rules: every row names its backing file and that file's
        hash, every field is a literal copy, and it can never close a gate."""
        m = self.manifest("evidence")
        self.assertEqual(len(m["derived_views"]), 1)
        view = m["derived_views"][0]
        self.assertEqual(view["path"], "EVIDENCE_INDEX.json")
        self.assertEqual(view["authority"], "screen_only")
        self.assertEqual(len(view["generator_sha256"]), 64)
        self.assertTrue(view["derived_from"])

        with zipfile.ZipFile(self.current / "fixture-evidence.zip") as zf:
            index = json.loads(zf.read("evidence/EVIDENCE_INDEX.json"))
            backing = {f["path"]: f for f in m["files"]}
            for row in index["rows"]:
                self.assertEqual(row["authority"], "screen_only")
                self.assertEqual(len(row["derived_from"]), 1)
                origin = row["derived_from"][0]
                self.assertTrue(origin.startswith(row["repo_path"] + "@sha256:"))
                digest = origin.split("@sha256:")[1]
                self.assertEqual(digest, backing[row["bundle_path"]]["sha256"])
                # Literal-copy rule: no field may appear that the source lacks.
                source = json.loads(zf.read(f"evidence/{row['bundle_path']}"))
                for key, value in row["fields"].items():
                    self.assertIn(key, source)
                    if isinstance(value, dict) and "__not_copied" in value:
                        continue
                    self.assertEqual(value, source[key])

    def test_an_oversized_index_value_is_marked_not_truncated(self):
        """Silent truncation would make the index quietly wrong about the
        measurement. Omission has to announce itself."""
        big = {"verdict": "GREEN", "violations": [{"id": i, "note": "x" * 40}
                                                  for i in range(50)]}
        self.write("board/build/big_receipt.json", json.dumps(big))
        proc = self.run_export(self.default_conf())
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        with zipfile.ZipFile(self.current / "fixture-evidence.zip") as zf:
            index = json.loads(zf.read("evidence/EVIDENCE_INDEX.json"))
        row = next(r for r in index["rows"]
                   if r["repo_path"] == "board/build/big_receipt.json")
        self.assertEqual(row["fields"]["verdict"], "GREEN")
        omitted = row["fields"]["violations"]
        self.assertIn("__not_copied", omitted)
        self.assertEqual(omitted["json_type"], "list")
        self.assertEqual(omitted["length"], 50)
        self.assertIn("violations", row["top_level_keys"])

    def test_derived_views_stay_out_of_the_coverage_arithmetic(self):
        """The index is generated, not staged from a source. Counting it as
        `scanned` would inflate coverage with the tool's own output."""
        m = self.manifest("evidence")
        paths = {f["path"] for f in m["files"]}
        self.assertNotIn("EVIDENCE_INDEX.json", paths)
        self.assertNotIn("MANIFEST.json", paths)
        self.assertEqual(
            m["coverage_files"]["scanned"] + m["coverage_files"]["excluded"],
            m["coverage_files"]["eligible"],
        )

    def test_a_bundle_with_no_evidence_json_gets_no_index(self):
        self.assertEqual(self.manifest("code")["derived_views"], [])

    def test_independence_defaults_to_unmeasured_not_to_true(self):
        r = self.receipt()
        self.assertFalse(r["independence"]["separate_session"])
        self.assertTrue(r["independence"]["note"])
        self.assertEqual(r["external_report_acceptance"], "CONFIRM")


class FaultInjection(ExportHarness):
    """Each test breaks one thing and asserts the export refuses to publish."""

    def assert_refused(self, proc, verdict: str):
        self.assertNotEqual(proc.returncode, 0,
                            msg=f"export succeeded but should have failed\n{proc.stdout}")
        self.assertIn(verdict, proc.stderr)
        self.assertFalse((self.out / "current").exists(),
                         "a refused run published a bundle set")

    def test_missing_required_dir_is_fatal(self):
        conf = self.write_conf(
            'define_bundle code "src" dir:src dir:does-not-exist file:README.md\n'
        )
        self.assert_refused(self.run_export(conf), "NO_COVERAGE")

    def test_missing_required_file_is_fatal(self):
        conf = self.write_conf(
            'define_bundle code "src" dir:src file:MISSING.md\n'
        )
        self.assert_refused(self.run_export(conf), "NO_COVERAGE")

    def test_no_env_var_can_downgrade_a_missing_source_to_a_warning(self):
        """The old behaviour was WARN + continue. If any of these revive it, the
        hard rule is decorative."""
        conf = self.write_conf('define_bundle code "src" dir:src dir:nope\n')
        for var in ("ALLOW_MISSING", "GPTPRO_ALLOW_MISSING", "SOFT_SOURCES"):
            with self.subTest(var=var):
                proc = self.run_export(conf, **{var: "1"})
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("NO_COVERAGE", proc.stderr)

    def test_optional_source_is_allowed_and_stays_visible_in_the_manifest(self):
        conf = self.write_conf(
            'define_bundle code "src" dir:src optional:dir:nope file:README.md\n'
        )
        proc = self.run_export(conf)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        m = self.manifest("code")
        self.assertIn("optional:dir:nope", m["optional_sources_absent"])
        absent = next(s for s in m["sources"] if s["decl"] == "optional:dir:nope")
        self.assertFalse(absent["present"])
        self.assertEqual(absent["disposition"], "optional")

    def test_a_bundle_that_stages_nothing_is_no_coverage(self):
        (self.root / "empty").mkdir()
        conf = self.write_conf('define_bundle void "nothing here" dir:empty\n')
        self.assert_refused(self.run_export(conf), "NO_COVERAGE")

    def test_restricted_class_smuggled_in_by_file_is_red(self):
        """J-5. `file:` used to bypass every exclusion, so the same .pdf was
        forbidden through `dir:` and free through `file:`. The content policy now
        runs on the staged tree, after inclusion, so the two paths meet."""
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "datasheet.pdf").write_bytes(b"%PDF-1.4 fake\n")
        conf = self.write_conf(
            'define_bundle code "src" dir:src file:vendor/datasheet.pdf\n'
        )
        proc = self.run_export(conf)
        self.assert_refused(proc, "RED")
        self.assertIn("restricted class", proc.stderr)

    def test_restricted_class_via_dir_is_excluded_not_a_violation(self):
        """The same file reached through a `dir:` source is dropped by the
        exclude and reported in the inventory — absent, not smuggled."""
        (self.root / "src" / "datasheet.pdf").write_bytes(b"%PDF-1.4 fake\n")
        conf = self.write_conf(
            "EXCLUDES+=( --exclude '*.pdf' )\n"
            'define_bundle code "src" dir:src\n'
        )
        proc = self.run_export(conf)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        m = self.manifest("code")
        pdf = next(e for e in m["exclusions"] if e["pattern"] == "*.pdf")
        self.assertEqual(pdf["policy_source"], "project_config")
        self.assertEqual(m["coverage_files"]["violations"], 0)

    def test_live_credential_aborts_the_export(self):
        self.write("src/leak.py", 'KEY = "ghp_' + "a" * 36 + '"\n')
        proc = self.run_export(self.default_conf())
        self.assert_refused(proc, "RED")

    def test_allow_secrets_publishes_but_can_never_be_green(self):
        self.write("src/leak.py", 'KEY = "ghp_' + "a" * 36 + '"\n')
        proc = self.run_export(self.default_conf(), ALLOW_SECRETS="1")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        r = self.receipt()
        self.assertNotEqual(r["verdict"], "GREEN")
        self.assertTrue(r["credential_scan"]["allow_secrets_override"])
        self.assertFalse(r["upload_authorized"])

    def test_two_sources_staging_onto_the_same_path_is_red(self):
        """Four `dir:<board>/build` roots collapsing onto one `build/` directory
        would silently overwrite three boards' erc.json with the fourth's."""
        conf = self.write_conf(
            'define_bundle code "src" dir:src file:src/app.py:src\n'
        )
        proc = self.run_export(conf)
        self.assert_refused(proc, "RED")
        self.assertIn("same path", proc.stderr)

    def test_denied_disclosure_rule_is_red(self):
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "authority": "test fixture",
            "decided_utc": "2026-01-01T00:00:00Z",
            "rules": [{"pattern": "src/app.py", "share": "deny",
                       "class": "RESTRICTED", "basis": "fixture"}],
        }), encoding="utf-8")
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assert_refused(proc, "RED")

    def _policy(self, *rules) -> Path:
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "authority": "test fixture operator",
            "decided_utc": "2026-01-01T00:00:00Z",
            "rules": list(rules),
        }), encoding="utf-8")
        return policy

    def test_confirm_class_publishes_but_never_reaches_green(self):
        """`share: confirm` is the third answer: somebody classified this and
        recorded that no authority has cleared it. It must not abort the export
        the way `deny` does, and it must not reach GREEN the way `allow` does."""
        policy = self._policy(
            {"pattern": "util.py", "share": "confirm",
             "class": "VENDOR_DERIVED", "basis": "fixture"},
            {"pattern": "*", "share": "allow", "class": "PROJECT_OWNED",
             "basis": "fixture"},
        )
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        gate = self.receipt()["disclosure_gate"]
        self.assertEqual(gate["status"], "CONFIRM")
        self.assertFalse(gate["upload_authorized"])
        self.assertEqual(gate["confirm_required"], 1)
        self.assertEqual(gate["confirm_classes"], ["VENDOR_DERIVED"])
        self.assertEqual(gate["unclassified"], 0)
        self.assertGreater(gate["allowed"], 0)
        self.assertEqual(gate["denied"], [])

    def test_confirm_and_unclassified_are_counted_separately(self):
        """Both land on CONFIRM, but 'we know we don't know' and 'we didn't
        look' must not share a number — otherwise a policy that covers nothing
        reads the same as one that covered everything and found a hard case."""
        policy = self._policy(
            {"pattern": "util.py", "share": "confirm",
             "class": "VENDOR_DERIVED", "basis": "fixture"},
        )
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        gate = self.receipt()["disclosure_gate"]
        self.assertEqual(gate["confirm_required"], 1)
        self.assertGreater(gate["unclassified"], 0)
        self.assertIn("UNKNOWN", gate["reason"])
        self.assertIn("VENDOR_DERIVED", gate["reason"])
        cov = gate["coverage_files"]
        self.assertEqual(cov["scanned"] + cov["excluded"], cov["eligible"])

    def test_first_matching_rule_wins_so_restrictive_rules_can_precede_broad(self):
        """The live policy depends on this: `pins`/`ground-truth` are confirm
        while the board root that contains them is allow."""
        policy = self._policy(
            {"pattern": "util.py", "share": "confirm", "class": "VENDOR_DERIVED",
             "basis": "fixture"},
            {"pattern": "src", "share": "allow", "class": "PROJECT_OWNED",
             "basis": "fixture"},
            {"pattern": "*", "share": "allow", "class": "PROJECT_OWNED",
             "basis": "fixture"},
        )
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        gate = self.receipt()["disclosure_gate"]
        self.assertEqual(gate["confirm_required"], 1)
        self.assertEqual(gate["confirm_sample"], ["src/util.py"])

    def test_an_unrecognised_share_value_is_rejected_not_guessed(self):
        """A typo must not silently become the permissive branch."""
        policy = self._policy(
            {"pattern": "*", "share": "alow", "class": "X", "basis": "typo"},
        )
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assert_refused(proc, "RED")
        self.assertIn("allow / deny / confirm", proc.stderr)

    def test_the_repo_policy_classifies_everything_and_stays_confirm(self):
        """The shipped .gptpro/disclosure-policy.json, exercised against a
        fixture shaped like this repo: vendor-derived subtrees inside otherwise
        project-owned board roots."""
        repo_policy = REPO.parent / ".gptpro" / "disclosure-policy.json"
        if not repo_policy.is_file():
            self.skipTest(f"no disclosure policy in this checkout: {repo_policy}")
        policy = json.loads(repo_policy.read_text(encoding="utf-8"))
        shares = {r["share"] for r in policy["rules"]}
        self.assertTrue(shares <= {"allow", "deny", "confirm"})
        self.assertTrue(policy["authority"] and "REPLACE ME" not in policy["authority"])
        patterns = [r["pattern"] for r in policy["rules"]]
        self.assertNotIn("*", patterns,
                         "a catch-all allow rule classifies the unknown; every file "
                         "added later would become silently shareable")
        # Restrictive rules must precede the board roots they sit inside.
        self.assertLess(patterns.index("pins"), patterns.index("cam-a-og05b"))
        self.assertLess(patterns.index("ground-truth"), patterns.index("proc-r-rk3588"))

    def test_unattributed_disclosure_policy_is_rejected(self):
        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "rules": [{"pattern": "*", "share": "allow"}],
        }), encoding="utf-8")
        proc = self.run_export(self.default_conf(), DISCLOSURE_POLICY=str(policy))
        self.assert_refused(proc, "RED")
        self.assertIn("authority", proc.stderr)

    def test_full_allow_policy_reaches_green_only_with_a_clean_tree(self):
        """The one path to GREEN, and the proof that dirty-tree and disclosure
        are independent conditions rather than one condition counted twice."""
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)

        policy = self.tmp / "policy.json"
        policy.write_text(json.dumps({
            "authority": "test fixture operator",
            "decided_utc": "2026-01-01T00:00:00Z",
            "rules": [{"pattern": "*", "share": "allow", "class": "PROJECT_OWNED",
                       "basis": "fixture"}],
        }), encoding="utf-8")

        conf = self.write_conf(
            "EXCLUDES+=( --exclude '.git' )\n"
            'define_bundle code "application source" dir:src file:README.md\n'
        )
        proc = self.run_export(conf, DISCLOSURE_POLICY=str(policy))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        r = self.receipt()
        self.assertEqual(r["verdict"], "GREEN", msg=r["verdict_reasons"])
        self.assertTrue(r["upload_authorized"])
        self.assertFalse(r["snapshot"]["dirty"])

        # ...and one uncommitted byte takes it straight back to CONFIRM.
        self.write("src/app.py", "print('changed')\n")
        proc = self.run_export(conf, DISCLOSURE_POLICY=str(policy))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        r = self.receipt()
        self.assertEqual(r["verdict"], "CONFIRM")
        self.assertTrue(r["snapshot"]["dirty"])
        self.assertTrue(r["snapshot"]["dirty_inventory_sha256"])


class Publish(ExportHarness):

    def test_a_failed_run_leaves_the_previous_set_intact(self):
        ok = self.run_export(self.default_conf())
        self.assertEqual(ok.returncode, 0, msg=ok.stderr)
        first = self.receipt()["snapshot_id"]
        first_zips = sorted(p.name for p in self.current.glob("*.zip"))

        broken = self.write_conf('define_bundle code "src" dir:src dir:gone\n')
        self.assertNotEqual(self.run_export(broken).returncode, 0)

        self.assertEqual(self.receipt()["snapshot_id"], first)
        self.assertEqual(sorted(p.name for p in self.current.glob("*.zip")), first_zips)

    def test_runs_are_kept_under_a_run_id_and_pruned(self):
        conf = self.default_conf()
        self.assertEqual(self.run_export(conf, KEEP_RUNS="1").returncode, 0)
        self.write("src/app.py", "print('v2')\n")
        self.assertEqual(self.run_export(conf, KEEP_RUNS="1").returncode, 0)
        runs = sorted((self.out / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertTrue((self.out / "current").resolve().samefile(runs[0]))

    def test_publish_works_when_tmpdir_is_a_different_filesystem(self):
        """The publish step is a rename, and a rename cannot cross filesystems.
        With /tmp on tmpfs and the repo on disk, assembling the set in TMPDIR
        would fail at the very last step, after all the work. Pointing TMPDIR
        somewhere else must not change the outcome."""
        alien = self.tmp / "alien-tmp"
        alien.mkdir()
        proc = self.run_export(self.default_conf(), TMPDIR=str(alien))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertTrue((self.current / "handoff_receipt.json").is_file())
        self.assertEqual(list(alien.iterdir()), [], "temp staging was left behind")

    def test_a_failed_run_leaves_no_half_built_set_in_the_output_dir(self):
        broken = self.write_conf('define_bundle code "src" dir:src dir:gone\n')
        self.assertNotEqual(self.run_export(broken).returncode, 0)
        leftovers = [p.name for p in self.out.iterdir()]
        self.assertEqual(leftovers, [], f"failed run left {leftovers} behind")

    def test_recheck_reports_stale_after_the_tree_moves(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.com"],
                       cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)

        conf = self.write_conf(
            "EXCLUDES+=( --exclude '.git' )\n"
            'define_bundle code "src" dir:src file:README.md\n'
        )
        self.assertEqual(self.run_export(conf).returncode, 0)

        env = dict(os.environ, PROJECT_ROOT=str(self.root), OUTPUT_DIR=str(self.out))
        fresh = subprocess.run(["bash", str(EXPORT_SH), "--recheck"],
                               capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(fresh.returncode, 0, msg=fresh.stderr)
        self.assertIn("GREEN", fresh.stdout)

        self.write("src/app.py", "print('drifted')\n")
        stale = subprocess.run(["bash", str(EXPORT_SH), "--recheck"],
                               capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(stale.returncode, 3)
        self.assertIn("STALE", stale.stdout)


class PatternSemantics(unittest.TestCase):
    """The exclusion matcher is the whole coverage story: a pattern that matches
    more than the config author thought is exactly how `--exclude fault` ate
    PROC-R's fault-proof JSON while its comment said 'fault images'."""

    def setUp(self) -> None:
        if not ENGINE.is_file():
            self.skipTest("skills/gptpro is not in this checkout")
        import importlib.util
        spec = importlib.util.spec_from_file_location("gptpro_export", ENGINE)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def test_bare_pattern_matches_any_component(self):
        m = self.mod.pattern_matches
        self.assertTrue(m("fault", "artifacts/fault/baseline.json"))
        self.assertTrue(m("fault", "fault"))
        self.assertFalse(m("fault", "artifacts/fault.txt"))
        self.assertFalse(m("fault", "artifacts/faults/x.json"))

    def test_glob_matches_basename(self):
        m = self.mod.pattern_matches
        self.assertTrue(m("*.pdf", "docs/vendor/sheet.pdf"))
        self.assertFalse(m("*.pdf", "docs/pdf/notes.md"))

    def test_leading_slash_anchors_at_the_source_root(self):
        m = self.mod.pattern_matches
        self.assertTrue(m("/build", "build/erc.json"))
        self.assertFalse(m("/build", "board/build/erc.json"))

    def test_multi_component_pattern_matches_a_contiguous_run(self):
        m = self.mod.pattern_matches
        self.assertTrue(m("test/__snapshots__", "src/test/__snapshots__/a.snap"))
        self.assertFalse(m("test/__snapshots__", "src/test/unit/__snapshots__/a.snap"))

    def test_source_root_name_is_not_part_of_the_matched_path(self):
        """`dir:board/build` must stage its contents even though `build` is a
        global exclude — the recovery mechanism the hardware config depends on."""
        m = self.mod.pattern_matches
        self.assertFalse(m("build", "erc.json"))
        self.assertTrue(m("build", "board/build/erc.json"))


if __name__ == "__main__":
    unittest.main()
