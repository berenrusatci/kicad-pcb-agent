#!/usr/bin/env python3
"""Write-path safety tests for scripts/sch/wireify.py and rewire.py.

WHY THIS FILE EXISTS: a rigorous coverage re-check (AST + real-import scan
over every test file) found these two modules genuinely untested - and they
are the two highest-risk untested modules in the tree, because BOTH MUTATE A
DESIGN FILE. ``sch_route.py``, the third, is covered by
``test_sch_route.py``.

WHAT IS PROVED HERE: the safety contract, not the wiring geometry.
``test_sch_wire.py`` already proves the pin-transform maths end to end
against real KiCad symbol libraries; that suite is skipped when the
libraries are absent. The contract below is what protects the user's file
and must hold on EVERY machine, so it is tested with the external
dependencies (kicad-cli netlist extraction, the KiCad-open guard) mocked:

  1. a dry run must not touch the file at all,
  2. a write must leave a ``.bak`` behind,
  3. if the netlist changed, the write must be ROLLED BACK from that
     ``.bak`` and reported as ``rolled_back`` - a schematic edit that
     silently alters connectivity is the worst failure these tools can
     have,
  4. the KiCad-open guard must run before any write (an open KiCad and a
     file writer clobber each other),
  5. generated UUIDs must be deterministic, so re-running does not churn
     the file.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCH_SCRIPTS, TOOLS  # noqa: E402,F401

import rewire as rw  # noqa: E402
import wireify as wf  # noqa: E402

from common.ids import det_uuid  # noqa: E402

# A schematic with no symbols and no labels: every code path that matters
# for the WRITE CONTRACT runs, without needing KiCad's symbol libraries.
EMPTY_SCH = (
    '(kicad_sch\n'
    '\t(version 20231120)\n'
    '\t(generator "eeschema")\n'
    '\t(uuid "00000000-0000-0000-0000-000000000001")\n'
    '\t(paper "A4")\n'
    '\t(lib_symbols)\n'
    ')\n'
)


class _Sch:
    """A throwaway .kicad_sch on disk."""

    def __init__(self, text: str = EMPTY_SCH):
        self._tmp = tempfile.TemporaryDirectory(prefix="sch_safety_")
        self.path = Path(self._tmp.name) / "board.kicad_sch"
        self.path.write_text(text, encoding="utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False

    @property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def bak(self) -> Path:
        return Path(str(self.path) + ".bak")


class DeterministicUuid(unittest.TestCase):
    """Re-running must not churn the file with fresh UUIDs."""

    def test_wireify_uuid_is_stable_for_the_same_inputs(self):
        a = wf._det_uuid("/tmp/board.kicad_sch", "U1-1")
        b = wf._det_uuid("/tmp/board.kicad_sch", "U1-1")
        self.assertEqual(a, b)

    def test_wireify_uuid_differs_per_tag(self):
        base = wf._det_uuid("/tmp/board.kicad_sch", "U1-1")
        self.assertNotEqual(base, wf._det_uuid("/tmp/board.kicad_sch", "U1-2"))

    def test_same_named_files_under_different_boards_do_not_collide(self):
        """Regression for the basename bug: this tree really does carry
        mipi/power/PCIe/hdmi .kicad_sch under several boards."""
        self.assertNotEqual(wf._det_uuid("boardA/sheets/mipi.kicad_sch", "U1-1"),
                            wf._det_uuid("boardB/sheets/mipi.kicad_sch", "U1-1"))

    def test_a_scratch_copy_keeps_the_canonical_uuid(self):
        """The other half of the fix: identity must survive the
        scratch -> promote round trip, or every run churns the file."""
        self.assertEqual(
            wf._det_uuid("proj/sheets/board.kicad_sch", "U1-1"),
            wf._det_uuid("proj/.scratch/20260805T0522Z/sheets/board.kicad_sch",
                         "U1-1"))

    def test_a_different_filename_does_change_the_uuid(self):
        self.assertNotEqual(wf._det_uuid("/tmp/board.kicad_sch", "U1-1"),
                            wf._det_uuid("/tmp/other.kicad_sch", "U1-1"))

    def test_wireify_uses_its_own_namespace(self):
        """A wire from wireify and one from rewire at the same tag must not
        collide - KiCad treats a duplicate UUID as the same object."""
        self.assertNotEqual(det_uuid("wireify", "/tmp/b.kicad_sch", "w1"),
                            det_uuid("rewire", "/tmp/b.kicad_sch", "w1"))


class WireifyDryRun(unittest.TestCase):
    def test_a_dry_run_does_not_touch_the_file(self):
        with _Sch() as sch:
            before = sch.text
            report = wf.wireify(str(sch.path), write=False)
            self.assertEqual(before, sch.text, "a dry run modified the file")
            self.assertFalse(sch.bak.exists(), "a dry run left a .bak behind")
        self.assertIn("labels_on_pins", report)

    def test_a_dry_run_never_consults_the_kicad_open_guard(self):
        with _Sch() as sch:
            with mock.patch.object(wf, "assert_kicad_closed") as guard:
                wf.wireify(str(sch.path), write=False)
            guard.assert_not_called()

    def test_a_dry_run_reports_no_write_verdict_keys(self):
        """netlist_identical/rolled_back only mean something after a write."""
        with _Sch() as sch:
            report = wf.wireify(str(sch.path), write=False)
        self.assertNotIn("netlist_identical", report)
        self.assertNotIn("rolled_back", report)


class WireifyWriteContract(unittest.TestCase):
    def test_a_write_leaves_a_bak_and_keeps_the_netlist(self):
        nets = {"GND": {"U1-1"}}
        with _Sch() as sch:
            with mock.patch.object(wf, "assert_kicad_closed"), \
                 mock.patch.object(wf, "netlist_nets", return_value=nets):
                report = wf.wireify(str(sch.path), write=True)
            self.assertTrue(sch.bak.exists(), "no .bak was written")
        self.assertTrue(report["netlist_identical"])
        self.assertNotIn("rolled_back", report)

    def test_fault_injection_a_changed_netlist_is_rolled_back(self):
        """THE critical property: connectivity must never change silently."""
        before_after = [{"GND": {"U1-1"}}, {"GND": {"U1-1", "U2-1"}}]
        with _Sch() as sch:
            original = sch.text
            with mock.patch.object(wf, "assert_kicad_closed"), \
                 mock.patch.object(wf, "netlist_nets",
                                   side_effect=before_after):
                report = wf.wireify(str(sch.path), write=True)
            self.assertEqual(original, sch.text,
                             "the file was NOT restored from .bak")
        self.assertFalse(report["netlist_identical"])
        self.assertTrue(report["rolled_back"])
        self.assertIn("GND", report["netlist_diff"])

    def test_the_kicad_open_guard_runs_before_any_write(self):
        """An open KiCad and a file writer clobber each other."""
        order = []
        with _Sch() as sch:
            def guard(path, force=False):
                order.append("guard")

            real_copyfile = shutil.copyfile

            def tracking_copyfile(src, dst):
                order.append("backup")
                return real_copyfile(src, dst)

            with mock.patch.object(wf, "assert_kicad_closed", side_effect=guard), \
                 mock.patch.object(wf, "netlist_nets", return_value={}), \
                 mock.patch.object(wf.shutil, "copyfile",
                                   side_effect=tracking_copyfile):
                wf.wireify(str(sch.path), write=True)
        self.assertEqual("guard", order[0],
                         f"the guard did not run first: {order}")

    def test_the_guard_refusal_aborts_the_write(self):
        with _Sch() as sch:
            original = sch.text
            with mock.patch.object(wf, "assert_kicad_closed",
                                   side_effect=RuntimeError("KiCad is open")):
                with self.assertRaises(RuntimeError):
                    wf.wireify(str(sch.path), write=True)
            self.assertEqual(original, sch.text, "the file was written anyway")
            self.assertFalse(sch.bak.exists())

    def test_force_is_passed_through_to_the_guard(self):
        """--force exists for scratch copies only; it must reach the guard
        rather than silently skipping it here."""
        with _Sch() as sch:
            with mock.patch.object(wf, "assert_kicad_closed") as guard, \
                 mock.patch.object(wf, "netlist_nets", return_value={}):
                wf.wireify(str(sch.path), write=True, force=True)
            _, kwargs = guard.call_args
            self.assertTrue(kwargs.get("force"))


class NoUnclosedFileHandles(unittest.TestCase):
    """REGRESSION: these modules used to do ``open(p).read()`` and
    ``open(p, "w").write(x)`` with no context manager, relying on CPython's
    refcount to close them.

    That is not cosmetic HERE specifically: the write is followed by
    ``shutil.copyfile`` on the SAME path in the rollback branch, and on
    Windows an open write handle makes that copy fail with PermissionError -
    i.e. the recovery path would break exactly when it is needed. The two
    tests below attack it from both sides: the static shape, and the actual
    warning Python raises when a handle is dropped unclosed.
    """

    MODULES = ("rewire.py", "wireify.py", "sch_wire.py")

    def test_no_module_drops_an_unclosed_handle(self):
        import re
        pattern = re.compile(r"open\([^)]*\)\s*\.\s*(read|write)\b")
        for name in self.MODULES:
            source = (SCH_SCRIPTS / name).read_text(encoding="utf-8")
            # Strip comments so the explanatory notes about the old pattern
            # do not trip the check they are explaining.
            code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
            with self.subTest(module=name):
                self.assertIsNone(pattern.search(code),
                                  f"{name} drops an unclosed file handle")

    def test_a_write_raises_no_resource_warning(self):
        """The dynamic proof: ResourceWarning is what Python emits when a
        file object is garbage-collected still open."""
        import warnings
        with _Sch() as sch:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with mock.patch.object(wf, "assert_kicad_closed"), \
                     mock.patch.object(wf, "netlist_nets", return_value={}):
                    wf.wireify(str(sch.path), write=True)
                import gc
                gc.collect()
            leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual([], [str(w.message) for w in leaks])


class RewireDryRun(unittest.TestCase):
    def test_a_dry_run_does_not_touch_the_file(self):
        with _Sch() as sch:
            before = sch.text
            rw.rewire(str(sch.path), write=False)
            self.assertEqual(before, sch.text, "a dry run modified the file")
            self.assertFalse(sch.bak.exists())

    def test_a_dry_run_never_consults_the_kicad_open_guard(self):
        with _Sch() as sch:
            with mock.patch.object(rw, "assert_kicad_closed") as guard:
                rw.rewire(str(sch.path), write=False)
            guard.assert_not_called()

    def test_a_dry_run_returns_a_report(self):
        with _Sch() as sch:
            report = rw.rewire(str(sch.path), write=False)
        self.assertIsInstance(report, dict)


class RewireWriteContract(unittest.TestCase):
    def test_a_write_leaves_a_bak(self):
        with _Sch() as sch:
            with mock.patch.object(rw, "assert_kicad_closed"), \
                 mock.patch.object(rw, "netlist_nets", return_value={}):
                rw.rewire(str(sch.path), write=True)
            self.assertTrue(sch.bak.exists(), "no .bak was written")

    def test_the_kicad_open_guard_refusal_aborts_the_write(self):
        with _Sch() as sch:
            original = sch.text
            with mock.patch.object(rw, "assert_kicad_closed",
                                   side_effect=RuntimeError("KiCad is open")):
                with self.assertRaises(RuntimeError):
                    rw.rewire(str(sch.path), write=True)
            self.assertEqual(original, sch.text, "the file was written anyway")


if __name__ == "__main__":
    unittest.main()
