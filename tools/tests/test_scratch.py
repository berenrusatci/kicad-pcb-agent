#!/usr/bin/env python3
"""Tests for common/scratch.py (ported from test_scratch_yonetimi.py).

The path-escape guard (``assert_within_scratch``) is a SECURITY control: the
tests below prove that both the canonical directory itself and a ``../``
escape are rejected.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import TOOLS  # noqa: E402,F401

from common.scratch import (  # noqa: E402
    SCRATCH_ROOT_NAME,
    assert_within_scratch,
    create_scratch,
    list_scratches,
    new_scratch_id,
    promote_to_canonical,
    scratch_root,
)


class ScratchCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # A nested project dir, so that ".." escapes have somewhere to go.
        self.root = Path(self._tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "projem.kicad_pro").write_text("pro", encoding="utf-8")
        (self.project / "projem.kicad_pcb").write_text("pcb-v1", encoding="utf-8")
        docs = self.project / "DOCS"
        docs.mkdir()
        (docs / "01.md").write_text("doc", encoding="utf-8")


class Creation(ScratchCase):
    def test_id_is_sortable_and_utc(self):
        first = new_scratch_id()
        self.assertTrue(first.endswith("Z"))
        self.assertEqual(len(first), len(new_scratch_id()))

    def test_files_are_copied(self):
        scratch = create_scratch(str(self.project), scratch_id="sid1")
        self.assertEqual(scratch_root(str(self.project)) / "sid1", scratch)
        self.assertEqual("pcb-v1", (scratch / "projem.kicad_pcb")
                         .read_text(encoding="utf-8"))
        self.assertTrue((scratch / "DOCS" / "01.md").exists())

    def test_canonical_files_are_untouched(self):
        """Direct proof of the 'canonical files are read only' guarantee."""
        canonical = self.project / "projem.kicad_pcb"
        before = canonical.read_text(encoding="utf-8")

        scratch = create_scratch(str(self.project), scratch_id="sid1")
        (scratch / "projem.kicad_pcb").write_text("pcb-v2-scratch-degisikligi",
                                                 encoding="utf-8")

        self.assertEqual(before, canonical.read_text(encoding="utf-8"))
        self.assertEqual("pcb-v1", canonical.read_text(encoding="utf-8"))

    def test_scratch_does_not_copy_itself(self):
        """.scratch/ must not be copied INTO a new scratch (infinite nesting)."""
        create_scratch(str(self.project), scratch_id="sid1")
        second = create_scratch(str(self.project), scratch_id="sid2")
        self.assertFalse((second / SCRATCH_ROOT_NAME).exists())

    def test_same_id_twice_raises(self):
        create_scratch(str(self.project), scratch_id="sid1")
        with self.assertRaises(FileExistsError):
            create_scratch(str(self.project), scratch_id="sid1")

    def test_missing_project_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            create_scratch(str(self.project / "yok"))


class EscapeGuard(ScratchCase):
    def test_valid_scratch_passes_silently(self):
        scratch = create_scratch(str(self.project), scratch_id="sid1")
        self.assertIsNone(assert_within_scratch(scratch, str(self.project)))

    def test_canonical_dir_is_rejected(self):
        """FAIL-CLOSED core: the canonical directory itself is not a scratch."""
        with self.assertRaises(ValueError):
            assert_within_scratch(self.project, str(self.project))

    def test_dotdot_escape_is_rejected(self):
        """SECURITY: a path that looks like it is under .scratch/ but climbs
        out with ../ must be refused."""
        create_scratch(str(self.project), scratch_id="sid1")
        escaped = scratch_root(str(self.project)) / "sid1" / ".." / ".." / ".."
        with self.assertRaises(ValueError):
            assert_within_scratch(escaped, str(self.project))
        with self.assertRaises(ValueError):
            assert_within_scratch(
                Path(str(self.project) + "/.scratch/../../elsewhere"),
                str(self.project))


class Promotion(ScratchCase):
    def test_rejects_a_source_outside_scratch(self):
        """Promotion must not become an arbitrary copy-into-canonical."""
        fake = self.root / "baska_yer"
        fake.mkdir()
        (fake / "x.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            promote_to_canonical(fake, str(self.project))
        self.assertFalse((self.project / "x.txt").exists())

    def test_copies_files_back(self):
        scratch = create_scratch(str(self.project), scratch_id="sid1")
        (scratch / "projem.kicad_pcb").write_text("pcb-v2-yukseltildi",
                                                 encoding="utf-8")
        promote_to_canonical(scratch, str(self.project))
        self.assertEqual("pcb-v2-yukseltildi",
                         (self.project / "projem.kicad_pcb")
                         .read_text(encoding="utf-8"))

    def test_does_not_copy_the_scratch_root(self):
        scratch = create_scratch(str(self.project), scratch_id="sid1")
        promote_to_canonical(scratch, str(self.project))
        self.assertFalse((self.project / SCRATCH_ROOT_NAME
                          / SCRATCH_ROOT_NAME).exists())


class Listing(ScratchCase):
    def test_empty_project_lists_nothing(self):
        self.assertEqual([], list_scratches(str(self.project)))

    def test_newest_first(self):
        create_scratch(str(self.project), scratch_id="20260101T000000000000Z")
        create_scratch(str(self.project), scratch_id="20260102T000000000000Z")
        self.assertEqual(["20260102T000000000000Z", "20260101T000000000000Z"],
                         list_scratches(str(self.project)))


if __name__ == "__main__":
    unittest.main()
