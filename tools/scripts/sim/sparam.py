#!/usr/bin/env python3
"""S-parameter production: Touchstone WRITING, mixed-mode conversion and
channel cascading.

backend: cli  (network: none; scikit-rf is a local library)

WHAT WAS MISSING
-------------------------------------------------------------------------
This bundle could READ a Touchstone file (``sim/openems.py``) and nothing
else. It could not write one, could not cascade a channel out of its
pieces, and - the important one - it deliberately refused to report the
DIFFERENTIAL result. ``evaluate_touchstone``'s own docstring says why:

    "this does NOT compute the DIFFERENTIAL (Sdd11/Sdd21, mixed-mode)
     result - skrf.Network.se2gmm()'s port-pairing convention (which
     physical port maps to which mode index) could not be confirmed
     without skrf installed in the source environment, and a wrong guess
     reported as Sdd11 would be worse than not reporting it"

That refusal was right. It is also now unnecessary: scikit-rf IS
installed, so the convention was MEASURED instead of guessed.

THE CONVENTION, AS MEASURED
-------------------------------------------------------------------------
The decisive probe is a 4-port containing nothing but two ideal through
lines. A line joining two ports that ``se2gmm`` puts in the SAME pair
cannot carry anything between pair 1 and pair 2, so ``Sdd21`` collapses; a
line joining ports in DIFFERENT pairs leaves it finite. Measured:

    lines join 1-2 and 3-4:   Sdd21 = -600 dB   (both lines inside a pair)
    lines join 1-3 and 2-4:   Sdd21 =    0 dB   (both lines cross pairs)
    lines join 1-4 and 2-3:   Sdd21 =    0 dB   (both lines cross pairs)

Only one pairing satisfies all three: **ports (1,2) are pair 1 and ports
(3,4) are pair 2** - i.e. ports ``2k-1`` and ``2k`` form pair ``k``. The
last row is what rules out the alternative ``{1,4},{2,3}``.

Confirmed a second way, against closed form rather than against a recorded
output: with a skew of Δ degrees between the two conductors,
``|Sdc21| = |sin(Δ/2)|`` and ``|Sdd21| = |cos(Δ/2)|``. At Δ = 20° that is
-15.206 dB / -0.133 dB, and the library agrees to 1e-3 dB. A wrong pairing
is BLIND to skew (stays at -600 dB), which makes this a discriminator
rather than a smoke test.

WORTH RECORDING: the first reading of the probe was WRONG. The labels
described where the LINES sat, not which PORTS were paired, and the two
happen to be inverses of each other. The self-test caught it immediately
because it asserts against closed form instead of against whatever the
library returned - which is the entire reason it is written that way.

Real ``.s4p`` files disagree about numbering: some are ordered by END
(1,2 = P/N at one end - what ``se2gmm`` wants), others by CONDUCTOR (1,2 =
both ends of the P line - the ordering that silently collapses). So
``to_mixed_mode`` takes the pairing EXPLICITLY and permutes the ports
itself. A caller states physical reality and never has to know the
library's convention.

Usage:
    sparam.py <channel.s4p> [--positives 1,2] [--negatives 3,4]
              [--mask mipi-csi2-dphy] [--list-masks]
              [--il-limit-db -6] [--rl-limit-db -10] [--mode-conv-limit-db -20]
              (limits are OPTIONAL and have NO defaults: with neither a mask
               nor explicit limits the gate reports NO_COVERAGE - #53)
              [--cascade a.s2p b.s2p ...] [--write out.s2p] [--json report.json]
    sparam.py --self-test

Exit: 0 PASS, 1 FAIL/NO_COVERAGE, 2 crash (run_cli).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import tline as tl  # noqa: E402
from common.findings import Finding, result, summarise  # noqa: E402
from common.report_io import diag, emit, run_cli, write_report  # noqa: E402

SCHEMA_VERSION = "sparam-report/v1"
TOOL_NAME = "sparam"

# The measured fact this module is built on. Stated here so a future skrf
# release that changes it breaks a named assumption instead of silently
# producing mirrored mode indices.
SE2GMM_CONVENTION = ("skrf.se2gmm(p): ports 2k-1 and 2k form pair k - (1,2) is pair 1, "
                     "(3,4) is pair 2 (measured, see module docstring)")

# Mixed-mode block indices produced by se2gmm for a 2-pair (4-port) network.
# Re-exported from common.tline so there is ONE definition: this module and
# sim/eye.py used to carry their own copies, which is how two tools end up
# reading different terms while both claiming to report Sdc21.
DD, DC, CD, CC = tl.DD, tl.DC, tl.CD, tl.CC

# Shared primitives live in common/tline.py. They were duplicated here and in
# scripts/sparam.py after two independent passes at #54; the two agreed on the
# measured se2gmm convention but not on everything else, so they are now one
# implementation with one set of tests.
to_mixed_mode = tl.to_mixed_mode
mixed_mode_db = tl.mixed_mode_db
cascade = tl.cascade
read_touchstone = tl.read_touchstone
write_touchstone = tl.write_touchstone


class SparamUnavailable(RuntimeError):
    """scikit-rf is not importable. Reported as NO_COVERAGE, never as a pass -
    "we could not look" and "we looked and it was fine" are different
    answers."""


def _skrf():
    try:
        import skrf as rf
    except ImportError as exc:  # pragma: no cover - exercised by the NO_COVERAGE path
        raise SparamUnavailable(
            "scikit-rf is not installed - S-parameter work did not run.") from exc
    return rf


# --------------------------------------------------------------------------- #
# 3b. Compliance masks (#53)
# --------------------------------------------------------------------------- #
MASKS_FILE = (Path(__file__).resolve().parents[2] / "fixtures" / "references"
              / "signal_compliance_masks.json")

TERMS = (("rl_db", "Sdd11", "ceiling", "return_loss_exceeded"),
         ("il_db", "Sdd21", "floor", "insertion_loss_exceeded"),
         ("sdc21_db", "Sdc21", "ceiling", "mode_conversion_exceeded"))


class MaskError(ValueError):
    """A mask that cannot be used as written. Always loud: a compliance gate
    that silently falls back to no mask is a gate that certifies nothing
    while looking like it certified something."""


@dataclass(frozen=True)
class ComplianceMask:
    """One interface's frequency/dB limits, with where they came from."""

    name: str
    interface: str
    status: str
    provenance: dict
    points: tuple[dict, ...]

    @property
    def hz_range(self) -> tuple[float, float]:
        return self.points[0]["hz"], self.points[-1]["hz"]

    def cite(self) -> str:
        parts = [self.provenance.get("standard") or "?"]
        for key in ("revision", "section"):
            if self.provenance.get(key):
                parts.append(str(self.provenance[key]))
        if self.provenance.get("accessed"):
            parts.append(f"accessed {self.provenance['accessed']}")
        return " | ".join(parts)

    def limit_at(self, hz: float, key: str) -> float | None:
        """Linear-in-dB interpolation. ``None`` outside the breakpoints.

        Returning None rather than clamping is deliberate: a mask extended
        past its own endpoints is a mask nobody wrote, and a verdict from it
        would be this tool's opinion wearing a standard's name.
        """
        usable = [p for p in self.points if key in p]
        if len(usable) < 2 or hz < usable[0]["hz"] or hz > usable[-1]["hz"]:
            return None
        for lower, upper in zip(usable, usable[1:]):
            if lower["hz"] <= hz <= upper["hz"]:
                span = upper["hz"] - lower["hz"]
                if span <= 0:
                    return float(lower[key])
                ratio = (hz - lower["hz"]) / span
                return float(lower[key] + ratio * (upper[key] - lower[key]))
        return None


def load_masks(path: Path | str | None = None) -> dict:
    """Read the mask registry. Refuses a mask that claims more than it has."""
    path = Path(path) if path else MASKS_FILE
    if not path.is_file():
        raise MaskError(f"mask registry not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MaskError(f"{path}: not JSON: {exc}") from exc
    out: dict[str, ComplianceMask] = {}
    for name, raw in (payload.get("masks") or {}).items():
        status = raw.get("status")
        if status not in ("ACTIVE", "CONFIRM", "FIXTURE"):
            raise MaskError(f"{name}: status must be ACTIVE/CONFIRM/FIXTURE, "
                            f"got {status!r}")
        points = tuple(raw.get("points") or ())
        if status == "CONFIRM" and points:
            raise MaskError(
                f"{name}: status CONFIRM means the mask is not known yet, but "
                "it carries points. Either it is sourced (ACTIVE) or it is "
                "not (CONFIRM with no points) - not both.")
        if status == "ACTIVE":
            missing = [k for k in ("revision", "section", "locator", "accessed")
                       if not raw.get(k)]
            if missing:
                raise MaskError(
                    f"{name}: an ACTIVE mask is a normative threshold and is "
                    f"missing {missing}. Without those a reader cannot check "
                    "the number against its source, which is the whole point "
                    "of the promotion rule.")
            if len(points) < 2:
                raise MaskError(
                    f"{name}: ACTIVE with {len(points)} breakpoint(s); a mask "
                    "needs at least two to define a band.")
        for index, (lower, upper) in enumerate(zip(points, points[1:])):
            if upper["hz"] <= lower["hz"]:
                raise MaskError(
                    f"{name}: breakpoints must ascend in hz; point {index + 2} "
                    f"is at {upper['hz']} after {lower['hz']}")
        out[name] = ComplianceMask(
            name=name, interface=raw.get("interface", name), status=status,
            provenance={k: raw.get(k) for k in
                        ("standard", "revision", "section", "locator",
                         "accessed", "needs")},
            points=points)
    return out


def _mask_finding(check: str, network, mask: ComplianceMask,
                  sdd21, sdd11, sdc21):
    """Grade every frequency point against the mask, or say why we cannot."""
    if mask.status == "CONFIRM":
        return Finding(
            check, "NO_COVERAGE", 0, [],
            f"mask '{mask.name}' ({mask.interface}) is status CONFIRM: the "
            f"limits are not in this repository yet. {mask.provenance.get('needs') or ''} "
            "Until they are, this channel is UNMEASURED against that standard "
            "- which is not the same as compliant.")

    measured = {"rl_db": sdd11, "il_db": sdd21, "sdc21_db": sdc21}
    violations: List[dict] = []
    covered = 0
    uncovered: List[float] = []
    for index, hz in enumerate(network.f):
        limits = {key: mask.limit_at(float(hz), key) for key, *_ in TERMS}
        if all(value is None for value in limits.values()):
            uncovered.append(float(hz))
            continue
        covered += 1
        for key, term, sense, issue in TERMS:
            limit = limits[key]
            if limit is None:
                continue
            value = float(measured[key][index])
            bad = value < limit if sense == "floor" else value > limit
            if bad:
                violations.append({
                    "issue": issue, "term": term, "hz": float(hz),
                    "measured_db": round(value, 3),
                    "limit_db": round(limit, 3),
                    "mask": mask.name})
    detail = (f"mask '{mask.name}' ({mask.interface}) [{mask.cite()}]: "
              f"{covered}/{len(network.f)} frequency points inside the mask's "
              f"band {mask.hz_range[0]:.4g}..{mask.hz_range[1]:.4g} Hz, "
              f"{len(violations)} violation(s). {SE2GMM_CONVENTION}.")
    if covered == 0:
        return Finding(
            check, "NO_COVERAGE", 0, [],
            detail + " NO frequency point fell inside the mask: the sweep and "
            "the mask do not overlap, so nothing was graded.")
    if uncovered:
        detail += (f" {len(uncovered)} point(s) outside the mask were NOT "
                   "graded; the mask says nothing there and extrapolating it "
                   "would invent a limit.")
    return result(check, covered, violations, detail)


# --------------------------------------------------------------------------- #
# 4. The channel gate
# --------------------------------------------------------------------------- #
def check_differential_channel(network, positives=None, negatives=None,
                               il_limit_db: float | None = None,
                               rl_limit_db: float | None = None,
                               mode_conv_limit_db: float | None = None,
                               mask: "ComplianceMask | None" = None):
    """Sdd21 / Sdd11 / Sdc21 against limits or a mask, as a ``Finding``.

    ``il_limit_db`` is a FLOOR (insertion loss worse than this fails),
    ``rl_limit_db`` and ``mode_conv_limit_db`` are CEILINGS. ``mask`` is the
    frequency-dependent form of the same three limits.

    THE DEFAULTS ARE GONE, AND THAT IS THE POINT. This function used to
    default to -6 / -10 / -20 dB while its own docstring said it "does not
    carry a mask of its own". It did: a caller that supplied nothing was
    silently graded against three numbers that name no standard, no revision
    and no clause, and a PASS from that comparison looked exactly like a
    PASS against a real specification. Supplying nothing now reports
    NO_COVERAGE, which is the honest reading of "nobody said what passing
    means". Issue #53.
    """
    check = "sparam_differential_channel"
    mixed = to_mixed_mode(network, positives, negatives)

    sdd21 = mixed_mode_db(mixed, DD, out_pair=1, in_pair=0)
    sdd11 = mixed_mode_db(mixed, DD, out_pair=0, in_pair=0)
    sdc21 = mixed_mode_db(mixed, DC, out_pair=1, in_pair=0)

    if mask is not None:
        return _mask_finding(check, network, mask, sdd21, sdd11, sdc21)

    scalars = (il_limit_db, rl_limit_db, mode_conv_limit_db)
    if all(limit is None for limit in scalars):
        return Finding(
            check, "NO_COVERAGE", 0, [],
            "no mask and no explicit limits: the channel was solved but "
            "nothing said what passing means. Pass --mask NAME (see "
            "fixtures/references/signal_compliance_masks.json) or give the "
            "three limits explicitly. Reporting a verdict here would grade "
            "the board against numbers no standard backs.")

    violations: List[dict] = []
    worst_il = min(sdd21)
    if il_limit_db is not None and worst_il < il_limit_db:
        violations.append({"issue": "insertion_loss_exceeded", "term": "Sdd21",
                           "worst_db": round(worst_il, 3), "limit_db": il_limit_db})
    worst_rl = max(sdd11)
    if rl_limit_db is not None and worst_rl > rl_limit_db:
        violations.append({"issue": "return_loss_exceeded", "term": "Sdd11",
                           "worst_db": round(worst_rl, 3), "limit_db": rl_limit_db})
    worst_conv = max(sdc21)
    if mode_conv_limit_db is not None and worst_conv > mode_conv_limit_db:
        violations.append({"issue": "mode_conversion_exceeded", "term": "Sdc21",
                           "worst_db": round(worst_conv, 3), "limit_db": mode_conv_limit_db,
                           "note": "differential-to-common conversion - usually intra-pair skew "
                                   "or asymmetric reference"})

    return result(check, len(network.f), violations,
                  f"{network.nports}-port -> {network.nports // 2} pair(s); "
                  f"worst Sdd21={worst_il:.3f}dB, Sdd11={worst_rl:.3f}dB, "
                  f"Sdc21={worst_conv:.3f}dB across {len(network.f)} frequencies. "
                  f"{SE2GMM_CONVENTION}.")


def check_single_ended_channel(network, il_limit_db: float | None = None,
                               rl_limit_db: float | None = None):
    """S21 / S11 against limits for a 2-port, as a ``Finding``.

    Separate from the differential gate on purpose. A 2-port has an even port
    count but no PAIR: its ports are the two ends of one conductor. Routing it
    through the differential check reports an Sdc21 for a structure that has
    nothing to convert into, which is a number that cannot mean anything.
    """
    check = "sparam_single_ended_channel"
    s21 = tl.to_db(network.s[:, 1, 0])
    s11 = tl.to_db(network.s[:, 0, 0])

    if il_limit_db is None and rl_limit_db is None:
        return Finding(
            check, "NO_COVERAGE", 0, [],
            "no limits given: the 2-port was solved but nothing said what "
            "passing means. Same reason as the differential gate (#53) - the "
            "old -6/-10 dB defaults named no standard.")

    violations: List[dict] = []
    worst_il = float(min(s21))
    if il_limit_db is not None and worst_il < il_limit_db:
        violations.append({"issue": "insertion_loss_exceeded", "term": "S21",
                           "worst_db": round(worst_il, 3),
                           "limit_db": il_limit_db})
    worst_rl = float(max(s11))
    if rl_limit_db is not None and worst_rl > rl_limit_db:
        violations.append({"issue": "return_loss_exceeded", "term": "S11",
                           "worst_db": round(worst_rl, 3),
                           "limit_db": rl_limit_db})

    return result(check, len(network.f), violations,
                  f"2-port single-ended; worst S21={worst_il:.3f}dB, "
                  f"S11={worst_rl:.3f}dB across {len(network.f)} frequencies. "
                  "No pair, so no mixed-mode terms are reported.")


# --------------------------------------------------------------------------- #
# 5. Test/self-test construction helpers
# --------------------------------------------------------------------------- #
def ideal_pair_network(freq_ghz: Sequence[float], phase_deg: float = 45.0,
                       skew_deg: float = 0.0, loss_db: float = 0.0,
                       by_conductor: bool = False):
    """Two uncoupled matched lines as a 4-port, with a known answer.

    ``by_conductor=True`` numbers the ports 1,2 = both ends of one
    conductor, which is exactly the ordering that gives the wrong
    mixed-mode result if it is fed to ``se2gmm`` unpermuted.

    Analytic truth for this network: ``|Sdd21| = |cos(skew/2)|`` and
    ``|Sdc21| = |sin(skew/2)|``.
    """
    rf = _skrf()
    import numpy as np

    freq = rf.Frequency.from_f(list(freq_ghz), unit="ghz")
    count = len(freq.f)
    amplitude = 10.0 ** (loss_db / 20.0)
    t_a = amplitude * np.exp(-1j * np.deg2rad(phase_deg)) * np.ones(count)
    t_b = amplitude * np.exp(-1j * np.deg2rad(phase_deg + skew_deg)) * np.ones(count)

    s = np.zeros((count, 4, 4), dtype=complex)
    if by_conductor:
        # Ports numbered by CONDUCTOR: 1,2 = both ends of the P line.
        # This is the ordering that silently collapses if fed unpermuted.
        a_in, a_out, b_in, b_out = 0, 1, 2, 3
    else:
        # Ports numbered by END: 1,2 = P/N at one end, 3,4 = P/N at the
        # other. This is what se2gmm wants.
        a_in, a_out, b_in, b_out = 0, 2, 1, 3
    s[:, a_in, a_out] = s[:, a_out, a_in] = t_a
    s[:, b_in, b_out] = s[:, b_out, b_in] = t_b
    return rf.Network(frequency=freq, s=s, z0=50)


# --------------------------------------------------------------------------- #
# 6. CLI
# --------------------------------------------------------------------------- #
def _self_test() -> int:
    try:
        _skrf()
    except SparamUnavailable:
        diag("SELF-TEST SKIPPED: scikit-rf not installed")
        return 0

    freqs = [1.0, 5.0, 10.0]

    # Zero skew: the mixed-mode answer is known before the library is asked.
    net = ideal_pair_network(freqs)
    mixed = to_mixed_mode(net)
    assert abs(mixed_mode_db(mixed, DD, 1, 0)[0]) < 1e-6, "zero-skew Sdd21 must be 0 dB"
    assert mixed_mode_db(mixed, DC, 1, 0)[0] < -100.0, "zero-skew Sdc21 must vanish"

    # Skew against CLOSED FORM, not against a recorded number.
    skew = 20.0
    skewed = to_mixed_mode(ideal_pair_network(freqs, skew_deg=skew))
    expected_conv = 20.0 * math.log10(abs(math.sin(math.radians(skew / 2))))
    expected_il = 20.0 * math.log10(abs(math.cos(math.radians(skew / 2))))
    assert abs(mixed_mode_db(skewed, DC, 1, 0)[0] - expected_conv) < 1e-3, "Sdc21 != |sin(d/2)|"
    assert abs(mixed_mode_db(skewed, DD, 1, 0)[0] - expected_il) < 1e-3, "Sdd21 != |cos(d/2)|"

    # The pairing must be stated, not assumed: the same physical network in
    # end-numbered port order gives a WRONG answer if fed unpermuted.
    # Ports numbered by conductor: 1,2 are the P line's two ends and 3,4 the
    # N line's, so the PAIRS are (1,3) at one end and (2,4) at the other -
    # positives=[1,2] names the P port of each pair, negatives=[3,4] the N.
    by_conductor = ideal_pair_network(freqs, by_conductor=True)
    wrong = to_mixed_mode(by_conductor)
    right = to_mixed_mode(by_conductor, positives=[1, 2], negatives=[3, 4])
    assert mixed_mode_db(wrong, DD, 1, 0)[0] < -100.0, "the unpermuted read should collapse"
    assert abs(mixed_mode_db(right, DD, 1, 0)[0]) < 1e-6, "the stated pairing should be right"

    # Limits are stated HERE rather than defaulted, because the point of
    # #53 is that an unstated limit is not a limit.
    finding = check_differential_channel(net, il_limit_db=-6.0)
    assert finding["status"] == "PASS", finding
    lossy = ideal_pair_network(freqs, loss_db=-12.0)
    assert check_differential_channel(
        lossy, il_limit_db=-6.0)["status"] == "FAIL", (
        "a -12 dB channel must fail a -6 dB floor")
    assert check_differential_channel(net)["status"] == "NO_COVERAGE", (
        "no mask and no limits must be NO_COVERAGE, never a pass")

    masks = load_masks()
    assert masks["mipi-csi2-dphy"].status == "CONFIRM"
    assert check_differential_channel(
        net, mask=masks["mipi-csi2-dphy"])["status"] == "NO_COVERAGE", (
        "an unsourced interface mask must not grade anything")
    fixture = masks["fixture-symmetric-3ghz"]
    assert check_differential_channel(net, mask=fixture)["status"] == "PASS", (
        "an ideal pair must pass the fixture mask")
    assert check_differential_channel(
        lossy, mask=fixture)["status"] == "FAIL", (
        "a -12 dB channel must fail the fixture mask's insertion-loss floor")

    diag("SELF-TEST PASS: measured se2gmm convention, closed-form skew agreement, "
        "explicit pairing, and the channel gate all verified")
    return 0


# --------------------------------------------------------------------------- #
# 6. Synthesis - build a network instead of reading one
# --------------------------------------------------------------------------- #
def synthesise_line(args) -> Tuple[object, dict]:
    """Cross-section (or explicit Z0/er_eff) + length -> a Network.

    This is the half that closes the loop with the 2-D field solver: without
    it the impedance work stopped at a single number and there was no way to
    ask what the trace does at frequency. The provenance dict records WHICH
    grade of evidence produced the line parameters, because a field solve and
    a hand-typed --z0 are not the same claim.
    """
    frequency = tl.frequency_grid(args.start_hz, args.stop_hz, args.points)
    conductor = ((args.r_ohm_per_m, args.r_reference_hz)
                 if args.r_ohm_per_m is not None else None)

    if args.geometry:
        from common.field2d import (
            coupled_microstrip, coupled_stripline, microstrip, solve_line,
            stripline,
        )
        shared = {"cells_per_feature": args.cells_per_feature,
                  "max_nodes": args.max_nodes}
        if args.geometry == "microstrip":
            section = microstrip(args.w, args.t, args.h, args.er, **shared)
        elif args.geometry == "coupled-microstrip":
            section = coupled_microstrip(args.w, args.s, args.t, args.h,
                                         args.er, **shared)
        elif args.geometry == "stripline":
            section = stripline(args.w, args.t, args.b_spacing, args.er,
                                **shared)
        else:
            section = coupled_stripline(args.w, args.s, args.t,
                                        args.b_spacing, args.er, **shared)
        solved = solve_line(section)
        provenance = {"source": "field_solver 2-D solve",
                      "geometry": args.geometry,
                      "grid_pitch_um": round(section.dx_mm * 1000.0, 4),
                      "nodes": section.node_count}
        if args.geometry.startswith("coupled"):
            odd = tl.LineSpec(solved.z_odd_ohm(), solved.er_eff_odd(),
                              args.length_mm, args.tan_delta, conductor)
            even = tl.LineSpec(solved.z_even_ohm(), solved.er_eff_even(),
                               args.length_mm, args.tan_delta, conductor)
            provenance.update({"z_odd_ohm": round(odd.z0_ohm, 4),
                               "z_even_ohm": round(even.z0_ohm, 4),
                               "z_diff_ohm": round(2 * odd.z0_ohm, 4),
                               "er_eff_odd": round(odd.er_eff, 5),
                               "delay_ps": round(odd.delay_ps(), 4)})
            return tl.coupled_line_network(frequency, odd, even,
                                           args.z0_port), provenance
        spec = tl.LineSpec(solved.z0_ohm(), solved.er_eff(), args.length_mm,
                           args.tan_delta, conductor)
        provenance.update({"z0_ohm": round(spec.z0_ohm, 4),
                           "er_eff": round(spec.er_eff, 5),
                           "delay_ps": round(spec.delay_ps(), 4)})
        return tl.single_line_network(frequency, spec,
                                      args.z0_port), provenance

    if args.z0 is None or args.er_eff is None:
        raise ValueError("--line without --geometry needs both --z0 and --er-eff")
    spec = tl.LineSpec(args.z0, args.er_eff, args.length_mm, args.tan_delta,
                       conductor)
    provenance = {"source": "explicit --z0/--er-eff, no field solve",
                  "z0_ohm": spec.z0_ohm, "er_eff": spec.er_eff,
                  "delay_ps": round(spec.delay_ps(), 4)}
    if args.z_even is not None:
        even = tl.LineSpec(args.z_even, args.er_eff_even or args.er_eff,
                           args.length_mm, args.tan_delta, conductor)
        provenance["z_diff_ohm"] = 2 * spec.z0_ohm
        provenance["z_even_ohm"] = even.z0_ohm
        return tl.coupled_line_network(frequency, spec, even,
                                       args.z0_port), provenance
    return tl.single_line_network(frequency, spec, args.z0_port), provenance


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("touchstone", nargs="?")
    ap.add_argument("--positives", help="1-based port numbers, e.g. 1,2")
    ap.add_argument("--negatives", help="1-based port numbers, e.g. 3,4")
    # No defaults. -6/-10/-20 used to sit here and graded every board that
    # passed no flags against numbers naming no standard. Omitting them now
    # reports NO_COVERAGE (#53).
    ap.add_argument("--il-limit-db", type=float, default=None)
    ap.add_argument("--rl-limit-db", type=float, default=None)
    ap.add_argument("--mode-conv-limit-db", type=float, default=None)
    ap.add_argument("--mask", help="mask name from the registry; see "
                                   "--list-masks")
    ap.add_argument("--masks-file", dest="masks_file",
                    help="override the mask registry path")
    ap.add_argument("--list-masks", action="store_true", dest="list_masks",
                    help="print the registry and exit")
    ap.add_argument("--cascade", nargs="+", help="2-port files to chain into one channel")
    ap.add_argument("--write", help="write the (cascaded) network as Touchstone")
    ap.add_argument("--json")
    ap.add_argument("--self-test", action="store_true")

    synth = ap.add_argument_group(
        "synthesis (--line): build a network from a cross-section")
    synth.add_argument("--line", action="store_true",
                       help="synthesise instead of reading a file")
    synth.add_argument("--length-mm", type=float, dest="length_mm")
    synth.add_argument("--geometry", choices=("microstrip", "coupled-microstrip",
                                              "stripline", "coupled-stripline"))
    for name in ("w", "s", "h", "er"):
        synth.add_argument(f"--{name}", type=float)
    synth.add_argument("--t", type=float, default=0.035)
    synth.add_argument("--b-spacing", type=float, dest="b_spacing")
    synth.add_argument("--cells-per-feature", type=int, default=3,
                       dest="cells_per_feature")
    synth.add_argument("--max-nodes", type=int, default=120_000, dest="max_nodes")
    synth.add_argument("--z0", type=float,
                       help="odd-mode Z0 when --z-even is also given")
    synth.add_argument("--er-eff", type=float, dest="er_eff")
    synth.add_argument("--z-even", type=float, dest="z_even")
    synth.add_argument("--er-eff-even", type=float, dest="er_eff_even")
    synth.add_argument("--z0-port", type=float, default=50.0, dest="z0_port")
    synth.add_argument("--tan-delta", type=float, default=0.0, dest="tan_delta",
                       help="laminate loss tangent; omitting it means LOSSLESS "
                            "and the report says so, because an absent loss "
                            "model reads as a low-loss result")
    synth.add_argument("--r-ohm-per-m", type=float, dest="r_ohm_per_m",
                       help="conductor resistance per metre at "
                            "--r-reference-hz; enables sqrt(f) skin scaling")
    synth.add_argument("--r-reference-hz", type=float, default=1e9,
                       dest="r_reference_hz")
    synth.add_argument("--start-hz", type=float, default=1e7, dest="start_hz")
    synth.add_argument("--stop-hz", type=float, default=2e10, dest="stop_hz")
    synth.add_argument("--points", type=int, default=201)

    a = ap.parse_args(argv)

    if a.self_test:
        return _self_test()

    if a.list_masks:
        registry = load_masks(a.masks_file)
        emit({"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
              "masks": [{"name": m.name, "interface": m.interface,
                         "status": m.status, "points": len(m.points),
                         "cite": m.cite(),
                         "needs": m.provenance.get("needs")}
                        for m in registry.values()]})
        return 0

    def ports(text):
        return [int(x) for x in text.split(",")] if text else None

    provenance: dict = {}
    try:
        if a.line:
            if a.length_mm is None:
                ap.error("--line requires --length-mm")
            network, provenance = synthesise_line(a)
        elif a.cascade:
            network = cascade([read_touchstone(p) for p in a.cascade])
        elif a.touchstone:
            network = read_touchstone(a.touchstone)
        else:
            ap.error("give a Touchstone file, --line, --cascade, or --self-test")

        selected_mask = None
        if a.mask:
            registry = load_masks(a.masks_file)
            if a.mask not in registry:
                raise MaskError(
                    f"unknown mask {a.mask!r}; known: {sorted(registry)}")
            selected_mask = registry[a.mask]

        written = str(write_touchstone(network, a.write)) if a.write else None
        if network.nports >= 4 and network.nports % 2 == 0:
            finding = check_differential_channel(
                network, ports(a.positives), ports(a.negatives),
                a.il_limit_db, a.rl_limit_db, a.mode_conv_limit_db,
                mask=selected_mask)
        elif network.nports == 2:
            # A 2-port is EVEN but has no differential pair: its two ports are
            # the two ENDS of one line. The earlier `nports % 2 == 0` test let
            # it through, and se2gmm then paired those ends as if they were
            # P and N - reporting a mode-conversion figure for a structure
            # that has no second conductor to convert into.
            finding = check_single_ended_channel(
                network, a.il_limit_db, a.rl_limit_db)
        else:
            finding = result(
                "sparam_differential_channel", 0, [],
                f"{network.nports}-port network has no complete pairs - "
                "mixed-mode not evaluated (NO_COVERAGE, not a pass).")
    except (SparamUnavailable, tl.TransmissionLineError) as exc:
        finding = result("sparam_differential_channel", 0, [], str(exc))
        written = None

    report = {"schema_version": SCHEMA_VERSION, "tool": TOOL_NAME,
              "convention": SE2GMM_CONVENTION, "written": written,
              "lossless": bool(a.line and a.tan_delta == 0.0
                               and a.r_ohm_per_m is None),
              "provenance": provenance,
              "findings": [finding], "summary": summarise([finding])}
    write_report(report, a.json)
    emit(report)
    return 0 if finding["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(run_cli(main, schema_version=SCHEMA_VERSION))
