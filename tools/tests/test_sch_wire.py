#!/usr/bin/env python3
"""sch_wire evidence tests, converted from a hand-rolled main() to unittest.

This suite does NOT check "did the script draw a line". It checks whether the
pin actually lands on the net in ``kicad-cli sch export netlist`` output, and
it proves the test is not empty by fault injection: break one wire and the
test MUST fail.

Requires kicad-cli, the stock KiCad symbol libraries, and KiCad to be closed.
Skipped rather than failed when those are unavailable.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import SCH_SCRIPTS  # noqa: E402,F401

SYMDIR = Path("/usr/share/kicad/symbols")
REQUIRED_LIBS = ("Device.kicad_sym", "power.kicad_sym", "Connector.kicad_sym")

HAVE_CLI = shutil.which("kicad-cli") is not None
HAVE_LIBS = all((SYMDIR / name).is_file() for name in REQUIRED_LIBS)

ORIENTS = [(r, m) for r in (0, 90, 180, 270) for m in ("", "x", "y")]

VBUS_Y, GND_Y = 152.4, 180.34


@unittest.skipUnless(HAVE_CLI, "kicad-cli not on PATH")
@unittest.skipUnless(HAVE_LIBS, f"KiCad symbol libraries not found in {SYMDIR}")
class SchWireEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sch_wire import SchBuilder, load_lib_symbols

        cls.SchBuilder = SchBuilder
        cls.DEV = load_lib_symbols(str(SYMDIR / "Device.kicad_sym"))
        cls.PWR = load_lib_symbols(str(SYMDIR / "power.kicad_sym"))
        cls.CON = load_lib_symbols(str(SYMDIR / "Connector.kicad_sym"))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sch_wire_")
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------ build
    def build(self, *, break_wire: bool = False):
        """12 orientations of a resistor, each pin wired and labelled.

        A wrong lib->sheet transform shows up as a broken net, not as a
        visibly wrong drawing - which is the whole point.
        """
        sch = self.SchBuilder(project="wiretest")
        sch.embed(self.DEV["R"], "Device:R")
        sch.embed(self.DEV["C"], "Device:C")
        sch.embed(self.PWR["GND"], "power:GND")
        sch.embed(self.PWR["PWR_FLAG"], "power:PWR_FLAG")
        sch.embed(self.CON["Conn_01x02_Pin"], "Connector:Conn_01x02_Pin")

        expected: dict[str, set[str]] = {}
        for i, (rot, mir) in enumerate(ORIENTS):
            x = 30.48 + (i % 4) * 45.72
            y = 30.48 + (i // 4) * 50.8
            ref = f"R{i + 1}"
            r = sch.place(ref, "Device:R", x, y, value="10k", rot=rot, mirror=mir)
            for pn in ("1", "2"):
                net = f"N{i + 1}{pn}"
                sch.label(r.pin(pn), net)
                expected[net] = {f"{ref}-{pn}"}

        c1 = sch.place("C1", "Device:C", 30.48, 165.1, value="100n")
        c2 = sch.place("C2", "Device:C", 71.12, 165.1, value="100n")
        j1 = sch.place("J1", "Connector:Conn_01x02_Pin", 137.16, 165.1,
                       value="PWR_IN")

        for c in (c1, c2):
            sch.wire([c.pin("1").xy, (c.pin("1").x, VBUS_Y)])
            sch.wire([c.pin("2").xy, (c.pin("2").x, GND_Y)])
        p1, p2 = j1.pin("1"), j1.pin("2")
        sch.wire([p1.xy, (p1.x, VBUS_Y)])
        sch.wire([p2.xy, (p2.x, GND_Y)])
        sch.wire([(30.48, VBUS_Y), (p1.x, VBUS_Y)])
        sch.wire([(30.48, GND_Y), (p2.x, GND_Y)])

        sch.label((30.48, VBUS_Y), "VBUS")
        sch.power((50.8, GND_Y), "power:GND", ref="#PWR001",
                  direction=(0, 1), rot=0)
        sch.pwr_flag((93.98, VBUS_Y), ref="#FLG001")
        sch.pwr_flag((93.98, GND_Y), ref="#FLG002")
        sch.auto_junctions()
        expected["VBUS"] = {"C1-1", "C2-1", "J1-1"}
        expected["GND"] = {"C1-2", "C2-2", "J1-2"}

        name = "broken.kicad_sch" if break_wire else "wiretest.kicad_sch"
        path = os.path.join(self.tmp, name)
        text = sch.render()
        if break_wire:
            # FAULT INJECTION: shift the PIN end of C1.1's stub by 1.27 mm.
            # The line still looks like it touches the pin; the net breaks.
            # (Moving the rail end is not enough: a wire end landing on the
            #  MIDDLE of another wire is a valid KiCad T-connection.)
            text = text.replace("(xy 30.48 161.29)", "(xy 30.48 160.02)", 1)
        Path(path).write_text(text, encoding="utf-8")
        return path, expected

    # ------------------------------------------------------------------ tests
    def test_wires_are_actually_emitted(self):
        path, _ = self.build()
        self.assertGreaterEqual(Path(path).read_text(encoding="utf-8").count("(wire"),
                                30)

    def test_netlist_proves_every_expected_net(self):
        from common.kicad_cli import verify_nets

        path, expected = self.build()
        self.assertEqual([], verify_nets(path, expected))

    def test_no_unconnected_nets(self):
        from common.kicad_cli import netlist_nets

        path, _ = self.build()
        unconnected = [n for n in netlist_nets(path)
                       if n.startswith("unconnected-")]
        self.assertEqual([], unconnected)

    def test_fault_injection_proves_the_test_is_not_empty(self):
        """Break one wire; the netlist check MUST notice."""
        from common.kicad_cli import verify_nets

        bad, _ = self.build(break_wire=True)
        problems = verify_nets(bad, {"VBUS": {"C1-1", "C2-1", "J1-1"}})
        self.assertNotEqual([], problems,
                            "test is empty: a broken wire went undetected")

    def test_erc_is_clean_apart_from_expected_isolated_labels(self):
        from common.kicad_cli import erc_violations, run_erc

        path, _ = self.build()
        violations = erc_violations(run_erc(path))
        errors = [v for v in violations if v.get("severity") == "error"]
        # Each test label is intentionally on a single pin in this fixture.
        unexpected = [v for v in violations
                      if v.get("severity") != "error"
                      and v.get("type") != "isolated_pin_label"]
        self.assertEqual([], errors, errors)
        self.assertEqual([], unexpected, unexpected)


class KicadClosedGuardTests(unittest.TestCase):
    """The guard that stops a writer from clobbering an open KiCad session."""

    def test_process_query_uses_a_real_alternation(self):
        """Regression for the pgrep pattern.

        ``pgrep -a -x "kicad|pcbnew|eeschema"`` DOES work on procps-ng, which
        anchors the ERE. It does NOT work on the BSD/macOS pgrep, where -x is
        an exact string match and the alternation is taken literally, so the
        guard silently never fires. The helper now queries each name
        separately, which behaves identically everywhere.
        """
        from common import kicad_cli

        self.assertIn("kicad", kicad_cli.KICAD_GUI_PROCESSES)
        self.assertIn("pcbnew", kicad_cli.KICAD_GUI_PROCESSES)
        self.assertIn("eeschema", kicad_cli.KICAD_GUI_PROCESSES)
        for name in kicad_cli.KICAD_GUI_PROCESSES:
            self.assertNotIn("|", name,
                             "names must be queried one at a time")

    @unittest.skipUnless(shutil.which("pgrep"), "pgrep unavailable")
    def test_guard_detects_a_process_named_like_kicad(self):
        """Spawn a process actually named 'kicad' and prove the guard fires."""
        import subprocess

        from common.kicad_cli import assert_kicad_closed

        source = shutil.which("sleep")
        if source is None:
            self.skipTest("no sleep binary")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "kicad"
            shutil.copy(source, fake)
            proc = subprocess.Popen([str(fake), "30"])
            try:
                with self.assertRaises(RuntimeError) as caught:
                    assert_kicad_closed(str(Path(tmp) / "b.kicad_sch"))
                self.assertIn("KiCad", str(caught.exception))
            finally:
                proc.kill()
                proc.wait()

    def test_force_skips_the_guard(self):
        from common.kicad_cli import assert_kicad_closed
        with tempfile.TemporaryDirectory() as tmp:
            assert_kicad_closed(str(Path(tmp) / "b.kicad_sch"), force=True)

    def test_lock_file_is_detected(self):
        from common.kicad_cli import assert_kicad_closed
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "b.kicad_sch"
            target.write_text("", encoding="utf-8")
            (Path(tmp) / "~b.kicad_sch.lck").write_text("", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                assert_kicad_closed(str(target))


if __name__ == "__main__":
    unittest.main()
