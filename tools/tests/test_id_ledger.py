"""The item-number high-water mark, enforced rather than requested.

WHY THIS EXISTS
---------------
``docs/KALDIGIMIZ-YER.md`` has carried the rule "a number is never reused"
since the #59 collision: the 2026-08-06 list's #59 was "gerber2ems + termal
FEM", Tur 13 handed the same number to electrothermal coupling, and the
original item did not move anywhere - it silently dropped rather than closing.
Nobody noticed because the rule was prose, and prose does not compare itself
against the document.

The cheap closure is a counter plus this test. The ledger line is the single
place a new item takes its number from, and the assertions below make it
impossible for that line to disagree with the document it lives in.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
LEDGER_DOC = DOCS / "KALDIGIMIZ-YER.md"


def setUpModule():
    if not LEDGER_DOC.is_file():
        raise unittest.SkipTest("docs/KALDIGIMIZ-YER.md bu checkout'ta yok")

LEDGER = re.compile(r"sonraki kullanılmamış madde numarası:\s*(\d+)")
ITEM = re.compile(r"#(\d+)")


def _text() -> str:
    return LEDGER_DOC.read_text(encoding="utf-8")


class IdLedgerTest(unittest.TestCase):

    def test_the_ledger_line_exists(self):
        # If this fails, the counter was deleted and the reuse rule is back to
        # being an honour system.
        self.assertRegex(_text(), LEDGER)

    def test_the_counter_is_above_every_number_the_document_uses(self):
        text = _text()
        nxt = int(LEDGER.search(text).group(1))
        used = {int(m) for m in ITEM.findall(text)}
        # Four-digit "numbers" are commit-ish noise, not item ids; the item
        # range has never gone near three digits.
        used = {n for n in used if n < 1000}
        self.assertTrue(used, "no item numbers found - the regex has drifted")
        collisions = sorted(n for n in used if n >= nxt)
        self.assertEqual(
            collisions, [],
            f"the ledger says the next unused number is {nxt}, but the "
            f"document already uses {collisions}. Either the counter was not "
            "advanced when an item was added, or a number was reused - which "
            "is the #59 failure this ledger exists to prevent")

    def test_the_two_d5_children_are_registered_and_distinct(self):
        # The whole point of splitting #59 was that one capability closing
        # must not make the other look closed. If these ever collapse back
        # into one number, that risk returns.
        text = _text()
        self.assertIn("#71", text)
        self.assertIn("#72", text)
        self.assertNotIn("#59 = ", text.split("Numara defteri")[-1])

    def test_the_reuse_rule_is_still_stated_next_to_the_counter(self):
        # The counter catches collisions; the sentence explains why a dropped
        # item keeps its identity. Losing either half loses the practice.
        text = _text()
        self.assertIn("yeniden kullanılmaz", text)


if __name__ == "__main__":
    unittest.main()
