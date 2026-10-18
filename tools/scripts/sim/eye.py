#!/usr/bin/env python3
"""Worst-case (peak-distortion) eye opening from a channel's S-parameters.

backend: cli  (network: none; scikit-rf is a local library)

WHY: this board carries LPDDR4x, PCIe, USB3 and MIPI D-PHY, and nothing in
the bundle could say whether a routed channel's eye is open. ``common/
tline.py`` and ``scripts/sparam.py`` produce the differential channel
response; this module turns that response into an eye opening, WITHOUT an
IBIS-AMI model that does not exist here.

WHAT THIS IS - AND IS NOT
-------------------------------------------------------------------------
This is PEAK DISTORTION ANALYSIS: the pulse response is sampled at UI
spacing, the largest sample is the cursor, and every other sample is
inter-symbol interference that an adversarial bit pattern could align
against you at once. So:

  * the number is a WORST-CASE BOUND, not a BER contour. A real eye
    measured at BER 1e-12 is WIDER, because the worst pattern is rare. A
    channel that passes here passes at any BER; one that fails here may
    still be usable, and needs a statistical or time-domain run to say so.
  * there is NO transmitter or receiver model: no equalisation (FFE/CTLE/
    DFE), no jitter, no crosstalk from other pairs, no package. Every one
    of those changes the answer, and inventing any of them would break the
    no-invented-numbers rule this bundle runs on.
  * the opening is reported as a FRACTION of the full differential swing.
    Volts need the driver's real swing, which this module is not told, so
    it does not guess one - pass ``--swing-v`` to also get volts.

VALIDATION (measured, not assumed)
-------------------------------------------------------------------------
An ideal matched lossless line has a pulse response that is exactly one
UI-wide rectangle, so the cursor is 1.0, every other UI-spaced sample is 0,
and the eye is the full swing. Measured through this pipeline:

    ideal, 20 ps delay:   cursor 1.0000, sum|ISI| 2e-5, eye 2.0000
    ideal, 250 ps delay:  cursor 1.0000, sum|ISI| 3e-5, eye 2.0000
    -6 dB at 40 GHz:      cursor 0.8541, sum|ISI| 0.133, eye 1.4426
    -20 dB at 40 GHz:     cursor 0.5945, sum|ISI| 0.363, eye 0.4628

The first two rows are the closed-form answer to 5 digits, and their
agreement with EACH OTHER matters as much: a pure delay must not change an
eye, so a pipeline that drifts with delay is broken even if one case looks
right.

Usage:
    eye.py <channel.s4p> --ui-ps 100 [--port-order per_end|per_line]
           [--min-height 0.4] [--min-width-ui 0.6] [--swing-v 0.8]
           [--json report.json]
    eye.py --self-test

Exit: 0 PASS, 1 FAIL/NO_COVERAGE, 2 crash (run_cli).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import tline as tl  # noqa: E402
from common.findings import result, summarise  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "eye-report/v1"
TOOL_NAME = "eye"

# How many UI either side of the cursor are summed as ISI. Long enough to
# cover the reflection tail of a realistic channel; the sum converges long
# before this on anything sane, and a truncated tail would UNDER-state the
# distortion, i.e. err toward a falsely open eye.
ISI_SPAN_UI = 40


@dataclass
class EyeResult:
    cursor: float
    isi_sum: float
    height: float               # fraction of full differential swing
    width_ui: float
    ui_ps: float
    sample_phase_ui: float      # where in the UI the height was measured

    @property
    def is_open(self) -> bool:
        return self.height > 0.0


def differential_2port(network, port_order: str = tl.SE2GMM_PORT_ORDER):
    """The Sdd block of a 4-port as a plain 2-port, ready for time domain.

    ``port_order`` is REQUIRED information that a Touchstone file does not
    record. ``common.tline`` measured what ``se2gmm`` expects and names it
    ``per_end``; a file written ``per_line`` and read as ``per_end`` takes
    |Sdd21| from 1 to 0, i.e. reports a dead channel for a perfect one. It
    is therefore declared, never sniffed.
    """
    mixed = tl.mixed_mode(network, port_order)
    return mixed.subnetwork([0, 1])       # [D1, D2] of [D1, D2, C1, C2]


def pulse_response(network, ui_ps: float) -> Tuple[Sequence[float], Sequence[float], float]:
    """``(t_ps, pulse, dt_ps)`` for a 2-port.

    The channel must reach DC. ``skrf`` only WARNS when it does not ("Step
    response will not be correct") and returns a plausible-looking wrong
    answer, so a network that starts above DC is extrapolated here
    explicitly rather than left to a warning nobody reads.

    The pulse is built as ``step(t) - step(t - UI)``, which is the response
    to one bit rather than to an edge.
    """
    import numpy as np

    if network.nports != 2:
        raise ValueError(f"pulse_response needs a 2-port, got {network.nports} "
                         "(use differential_2port for a pair)")
    if ui_ps <= 0:
        raise ValueError(f"ui_ps must be positive, got {ui_ps}")

    work = network if network.f[0] == 0 else network.extrapolate_to_dc(kind="linear")
    t, step = work.step_response()
    y = np.asarray(step)[:, 1, 0].real
    t_ps = np.asarray(t) * 1e12
    dt = float(t_ps[1] - t_ps[0])
    shift = int(round(ui_ps / dt))
    if shift < 4:
        raise ValueError(
            f"only {shift} time sample(s) per UI - the frequency axis does not reach "
            f"high enough to resolve a {ui_ps} ps bit. Extend the S-parameters or "
            "the eye would be an artefact of the sampling, not of the channel.")
    delayed = np.concatenate([np.zeros(shift), y[:-shift]])
    return t_ps, y - delayed, dt


def _opening_at(pulse, index: int, samples_per_ui: float) -> Tuple[float, float]:
    """``(cursor, sum|ISI|)`` when the sampling instant is ``index``."""
    cursor = float(pulse[index])
    isi = 0.0
    for k in range(1, ISI_SPAN_UI + 1):
        for probe in (index + k * samples_per_ui, index - k * samples_per_ui):
            probe_i = int(round(probe))
            if 0 <= probe_i < len(pulse):
                isi += abs(float(pulse[probe_i]))
    return cursor, isi


def peak_distortion_eye(t_ps, pulse, ui_ps: float, dt_ps: float) -> EyeResult:
    """Worst-case eye height and width by sweeping the sampling phase.

    Height is taken at the BEST phase (the receiver is allowed to sample
    where the eye is widest open); width is the span of phases where the
    worst-case opening is still positive. Both are worst case over the DATA
    PATTERN, which is the bound this analysis provides.
    """
    import numpy as np

    samples_per_ui = ui_ps / dt_ps
    centre = int(np.argmax(np.abs(np.asarray(pulse))))
    half = int(round(samples_per_ui / 2))

    best_height, best_phase, open_samples = -float("inf"), 0.0, 0
    for offset in range(-half, half + 1):
        index = centre + offset
        if not 0 <= index < len(pulse):
            continue
        cursor, isi = _opening_at(pulse, index, samples_per_ui)
        opening = 2.0 * (abs(cursor) - isi)
        if opening > 0:
            open_samples += 1
        if opening > best_height:
            best_height, best_phase = opening, offset / samples_per_ui
            best_cursor, best_isi = cursor, isi

    if best_height == -float("inf"):  # pragma: no cover - guarded by pulse_response
        raise ValueError("no valid sampling phase inside the pulse response")

    return EyeResult(cursor=best_cursor, isi_sum=best_isi,
                     height=max(best_height, 0.0),
                     width_ui=open_samples / samples_per_ui,
                     ui_ps=ui_ps, sample_phase_ui=best_phase)


def check_eye(network, ui_ps: float, port_order: str = tl.SE2GMM_PORT_ORDER,
              min_height: float = 0.0, min_width_ui: float = 0.0,
              swing_v: Optional[float] = None):
    """The eye against caller-supplied minimums, as a ``Finding``.

    The limits are inputs, not defaults with opinions: an eye mask belongs
    to the interface standard (PCIe, USB3, MIPI D-PHY each differ), not to
    a generic tool. ``min_height``/``min_width_ui`` default to 0, which
    only asks "is the eye open at all".
    """
    check = "channel_eye_opening"
    two_port = differential_2port(network, port_order) if network.nports > 2 else network
    t_ps, pulse, dt = pulse_response(two_port, ui_ps)
    eye = peak_distortion_eye(t_ps, pulse, ui_ps, dt)

    violations: List[dict] = []
    if not eye.is_open:
        violations.append({"issue": "eye_closed", "height": round(eye.height, 5),
                           "cursor": round(eye.cursor, 5), "isi_sum": round(eye.isi_sum, 5),
                           "note": "worst-case ISI exceeds the cursor - no sampling phase "
                                   "survives an adversarial bit pattern"})
    else:
        if eye.height < min_height:
            violations.append({"issue": "eye_height_below_limit",
                               "height": round(eye.height, 5), "limit": min_height})
        if eye.width_ui < min_width_ui:
            violations.append({"issue": "eye_width_below_limit",
                               "width_ui": round(eye.width_ui, 5), "limit": min_width_ui})

    volts = f", {eye.height * swing_v:.4f} V of a {swing_v} V swing" if swing_v else ""
    return result(check, len(pulse), violations,
                  f"worst-case (peak-distortion) eye at {ui_ps} ps UI: "
                  f"height {eye.height:.4f} of full swing{volts}, width {eye.width_ui:.4f} UI, "
                  f"cursor {eye.cursor:.4f}, sum|ISI| {eye.isi_sum:.4f}. BOUND, not a BER "
                  "contour; no equaliser, jitter, crosstalk or package included.")


# --------------------------------------------------------------------------- #
# Construction helper - a channel with a KNOWN answer
# --------------------------------------------------------------------------- #
def ideal_channel(delay_ps: float, loss_db_at_fmax: float = 0.0,
                  f_max_ghz: float = 40.0, points: int = 801):
    """Matched 2-port: pure delay, optional sqrt(f) loss shape.

    Lossless, this has a rectangular pulse response - cursor 1, zero ISI,
    full eye - which is what makes it a control rather than a demo. Built
    from an explicit S-matrix rather than through ``skrf.media``, whose
    propagation constant divides by zero at DC and returns NaN - and the
    step response NEEDS DC.
    """
    import skrf as rf
    import numpy as np

    freq = rf.Frequency.from_f(np.linspace(0.0, f_max_ghz, points), unit="ghz")
    omega = 2 * np.pi * freq.f * 1e9
    t21 = np.exp(-1j * omega * delay_ps * 1e-12)
    if loss_db_at_fmax:
        t21 = t21 * 10 ** (loss_db_at_fmax / 20.0 * np.sqrt(freq.f / freq.f[-1]))
    s = np.zeros((points, 2, 2), dtype=complex)
    s[:, 0, 1] = s[:, 1, 0] = t21
    return rf.Network(frequency=freq, s=s, z0=50)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    if not tl.HAVE_SKRF:
        diag("SELF-TEST SKIPPED: scikit-rf not installed")
        return 0

    ui = 100.0

    # Closed form: an ideal lossless channel is a full, clean eye.
    t, pulse, dt = pulse_response(ideal_channel(20.0), ui)
    eye = peak_distortion_eye(t, pulse, ui, dt)
    assert abs(eye.cursor - 1.0) < 1e-3, eye
    assert eye.isi_sum < 1e-3, eye
    assert abs(eye.height - 2.0) < 1e-2, eye

    # A pure delay must not change an eye - agreement between these two is
    # as strong a check as either one alone.
    t2, pulse2, dt2 = pulse_response(ideal_channel(250.0), ui)
    eye2 = peak_distortion_eye(t2, pulse2, ui, dt2)
    assert abs(eye2.height - eye.height) < 1e-3, (eye, eye2)

    # Loss must close the eye, monotonically.
    heights = []
    for loss in (0.0, -6.0, -20.0):
        ta, pa, da = pulse_response(ideal_channel(20.0, loss_db_at_fmax=loss), ui)
        heights.append(peak_distortion_eye(ta, pa, ui, da).height)
    assert heights[0] > heights[1] > heights[2], heights

    # A UI the frequency axis cannot resolve is refused, not reported.
    try:
        pulse_response(ideal_channel(20.0, f_max_ghz=1.0, points=51), 1.0)
        raise AssertionError("an unresolvable UI must raise, not return an artefact")
    except ValueError:
        pass

    finding = check_eye(ideal_channel(20.0), ui, min_height=1.0)
    assert finding["status"] == "PASS", finding
    tight = check_eye(ideal_channel(20.0, loss_db_at_fmax=-20.0), ui, min_height=1.0)
    assert tight["status"] == "FAIL", tight

    diag("SELF-TEST PASS: closed-form ideal eye, delay independence, monotonic loss "
        "closure, unresolvable-UI refusal and the limit gate all verified")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("touchstone", nargs="?")
    ap.add_argument("--ui-ps", type=float, help="unit interval in ps (1/bit rate)")
    ap.add_argument("--port-order", default=tl.SE2GMM_PORT_ORDER,
                    choices=("per_end", "per_line"),
                    help="Touchstone records no port order; declare the file's")
    ap.add_argument("--min-height", type=float, default=0.0)
    ap.add_argument("--min-width-ui", type=float, default=0.0)
    ap.add_argument("--swing-v", type=float, default=None)
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()
    if not a.touchstone:
        ap.error("give a Touchstone file or --self-test")
    if a.ui_ps is None:
        ap.error("--ui-ps is required: the eye is defined per bit, and a default "
                 "would silently analyse a bit rate nobody chose")

    if not tl.HAVE_SKRF:
        finding = result("channel_eye_opening", 0, [],
                         "scikit-rf is not installed - the eye was not computed "
                         "(NO_COVERAGE, not a pass).")
    else:
        import skrf as rf
        finding = check_eye(rf.Network(a.touchstone), a.ui_ps, a.port_order,
                            a.min_height, a.min_width_ui, a.swing_v)

    report = {"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
              "findings": [finding], "summary": summarise([finding])}
    write_report(report, a.json)
    emit(report)
    return 0 if finding["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
