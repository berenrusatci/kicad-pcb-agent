"""Transmission-line S-parameter synthesis from a solved cross-section.

WHY THIS EXISTS
---------------
``scripts/sim/openems.py`` can READ a Touchstone file and check return loss,
but nothing in this toolchain could PRODUCE one. That left the impedance work
stopping at a single number: ``field_solver`` says a pair is 104 ohm, and no
tool could then answer what the insertion loss is at 2.5 GHz, or what happens
when that pair passes through a connector and a second board.

This module turns a solved cross-section into a frequency-dependent network.
The chain is field solve -> per-unit-length L and C -> propagation constant ->
S-parameters, and every step is an identity for a TEM line rather than a fit.

THE PORT-ORDER QUESTION, NOW MEASURED
-------------------------------------
``evaluate_touchstone``'s docstring recorded a deliberate boundary: it refused
to compute mixed-mode results because ``skrf.Network.se2gmm()``'s port-pairing
convention "could not be confirmed without skrf installed in the source
environment, and a wrong guess reported as Sdd11 would be worse" than not
answering. skrf 2.0.1 is now installed, so it was MEASURED.

The decisive probe is a 4-port containing nothing but two ideal through lines.
A line joining two ports that ``se2gmm`` puts in the SAME pair cannot carry
anything between pair 1 and pair 2, so Sdd21 collapses; a line joining ports
in DIFFERENT pairs leaves it finite:

    lines join 1-2 and 3-4:   Sdd21 = -600 dB   (both lines inside a pair)
    lines join 1-3 and 2-4:   Sdd21 =    0 dB   (both lines cross pairs)
    lines join 1-4 and 2-3:   Sdd21 =    0 dB   (both lines cross pairs)

Only one pairing satisfies all three: ports (1,2) are pair 1 and (3,4) are
pair 2, i.e. ports ``2k-1`` and ``2k`` form pair ``k``. The third row is what
rules out the alternative {1,4},{2,3} - a two-layout probe cannot.

Cross-checked against closed form rather than against a recorded output: with
a skew of D degrees between the conductors, ``|Sdc21| = |sin(D/2)|`` and
``|Sdd21| = |cos(D/2)|``, and the library agrees to 1e-3 dB at D = 20 deg. A
wrong pairing is BLIND to skew (stays at -600 dB), which makes this a
discriminator rather than a smoke test.

Real ``.s4p`` files disagree about numbering: some are ordered by END (1,2 =
P/N at one end - what se2gmm wants), others by CONDUCTOR (1,2 = both ends of
the P line - the ordering that silently collapses). Touchstone records none of
this, so ``to_mixed_mode`` takes the pairing EXPLICITLY and permutes the ports
itself; the caller states physical reality and never has to know the library's
convention.

WHAT IS AND IS NOT MODELLED
---------------------------
Modelled: a uniform TEM line of a given length, with the impedance and
effective permittivity from the 2-D solve, and optional dielectric loss from
an explicitly supplied loss tangent.

NOT modelled, and none of it is silently approximated: vias and their stubs,
connector transitions, launch discontinuities, bends, non-uniform width,
conductor loss (unless an explicit R' is supplied), surface roughness,
frequency-dependent er, fibre-weave variation, and crosstalk from any third
conductor. A channel built from this is the TRACE, not the link.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .units import C_MPS

try:                                    # pragma: no cover - import guard
    import skrf
    HAVE_SKRF = True
except ImportError:                     # pragma: no cover - import guard
    skrf = None                         # type: ignore[assignment]
    HAVE_SKRF = False

MODEL_CLASS = "tem_line_from_2d_solve"
SIGNOFF = False

# Measured, not assumed - see the module docstring.
SE2GMM_PORT_ORDER = "per_end"
MIXED_MODE_PORT_ORDER = ("D1", "D2", "C1", "C2")


class TransmissionLineError(ValueError):
    """The line specification cannot produce a network."""


def _require_skrf() -> None:
    if not HAVE_SKRF:
        raise TransmissionLineError(
            "scikit-rf is not installed; no network is fabricated")


# --------------------------------------------------------------------------- #
# Propagation
# --------------------------------------------------------------------------- #
def phase_constant(frequency_hz: np.ndarray, er_eff: float) -> np.ndarray:
    """beta = omega * sqrt(er_eff) / c, radians per metre. Exact for TEM."""
    if er_eff <= 0:
        raise TransmissionLineError(f"er_eff must be positive, got {er_eff}")
    return 2.0 * math.pi * np.asarray(frequency_hz, dtype=float) * math.sqrt(
        er_eff) / C_MPS


def dielectric_attenuation(frequency_hz: np.ndarray, er_eff: float,
                           tan_delta: float) -> np.ndarray:
    """alpha_d = omega * tan(delta) * sqrt(er_eff) / (2c), nepers per metre.

    This is the low-loss form, obtained from alpha_d = (G/2)*sqrt(L/C) with
    G = omega*C*tan(delta), so it is derived from the loss tangent rather than
    fitted. tan_delta is never defaulted: a laminate's loss tangent is a
    datasheet number and guessing it would silently set the insertion loss.
    """
    if tan_delta < 0:
        raise TransmissionLineError(f"tan_delta must be >= 0, got {tan_delta}")
    return (2.0 * math.pi * np.asarray(frequency_hz, dtype=float) * tan_delta
            * math.sqrt(er_eff) / (2.0 * C_MPS))


def conductor_attenuation(frequency_hz: np.ndarray, z0_ohm: float,
                          r_ohm_per_m_at_hz: tuple[float, float] | None
                          ) -> np.ndarray:
    """alpha_c = R'(f) / (2*Z0), with R' scaled as sqrt(f) from a stated point.

    Returns zeros when no reference resistance is supplied. The sqrt(f) skin
    scaling is an APPROXIMATION and is labelled as one in the report; the
    reference R' itself must be measured or taken from a datasheet, because
    deriving it from geometry alone would need a surface-roughness model this
    module does not have.
    """
    frequency = np.asarray(frequency_hz, dtype=float)
    if r_ohm_per_m_at_hz is None:
        return np.zeros_like(frequency)
    resistance, reference_hz = r_ohm_per_m_at_hz
    if resistance < 0 or reference_hz <= 0:
        raise TransmissionLineError(
            "conductor loss needs a non-negative R' and a positive reference "
            f"frequency, got {r_ohm_per_m_at_hz}")
    if z0_ohm <= 0:
        raise TransmissionLineError(f"z0 must be positive, got {z0_ohm}")
    scaled = resistance * np.sqrt(frequency / reference_hz)
    return scaled / (2.0 * z0_ohm)


@dataclass
class LineSpec:
    """One propagating mode: its impedance, permittivity and loss."""

    z0_ohm: float
    er_eff: float
    length_mm: float
    tan_delta: float = 0.0
    r_ohm_per_m_at_hz: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if self.z0_ohm <= 0:
            raise TransmissionLineError(f"z0 must be positive, got {self.z0_ohm}")
        if self.er_eff <= 0:
            raise TransmissionLineError(
                f"er_eff must be positive, got {self.er_eff}")
        if self.length_mm <= 0:
            raise TransmissionLineError(
                f"length must be positive, got {self.length_mm}")

    def gamma(self, frequency_hz: np.ndarray) -> np.ndarray:
        alpha = (dielectric_attenuation(frequency_hz, self.er_eff,
                                        self.tan_delta)
                 + conductor_attenuation(frequency_hz, self.z0_ohm,
                                         self.r_ohm_per_m_at_hz))
        return alpha + 1j * phase_constant(frequency_hz, self.er_eff)

    def delay_ps(self) -> float:
        """One-way propagation delay in picoseconds."""
        return (self.length_mm * 1e-3) * math.sqrt(self.er_eff) / C_MPS * 1e12

    def is_lossless(self) -> bool:
        return self.tan_delta == 0.0 and self.r_ohm_per_m_at_hz is None


# --------------------------------------------------------------------------- #
# Network construction
# --------------------------------------------------------------------------- #
def frequency_grid(start_hz: float, stop_hz: float, points: int):
    """A skrf Frequency object. Linear spacing: a channel response is read at
    evenly spaced points, unlike a filter response which wants a decade sweep."""
    _require_skrf()
    if not (start_hz > 0 and stop_hz > start_hz and points >= 2):
        raise TransmissionLineError(
            f"need 0 < start < stop and points >= 2, got "
            f"{start_hz}, {stop_hz}, {points}")
    return skrf.Frequency.from_f(
        np.linspace(start_hz, stop_hz, int(points)), unit="hz")


def single_line_network(frequency, spec: LineSpec, z0_port: float = 50.0):
    """A 2-port for one uniform line, referenced to ``z0_port``.

    Built through skrf's DefinedGammaZ0 media rather than by hand-writing the
    ABCD matrix, so the S-parameter conversion and renormalisation come from
    the library that will also read the file back.
    """
    _require_skrf()
    media = skrf.media.DefinedGammaZ0(
        frequency=frequency, z0_port=z0_port,
        z0=spec.z0_ohm, gamma=spec.gamma(frequency.f))
    network = media.line(spec.length_mm, unit="mm")
    network.name = f"line_{spec.z0_ohm:.1f}ohm_{spec.length_mm:.3f}mm"
    return network


def coupled_line_network(frequency, odd: LineSpec, even: LineSpec,
                         z0_port: float = 50.0):
    """A 4-port for a symmetric coupled pair, in SINGLE-ENDED port order
    [A_in, B_in, A_out, B_out].

    Construction is modal: the differential mode is a line of impedance
    2*Z_odd and the common mode a line of Z_even/2, they are placed
    block-diagonally in the [D1, D2, C1, C2] order that was MEASURED to be
    skrf's mixed-mode ordering, and ``gmm2se`` converts to single-ended. The
    two modes are independent for a symmetric pair, which is exactly why the
    block-diagonal form is exact here rather than an approximation.
    """
    _require_skrf()
    if odd.length_mm != even.length_mm:
        raise TransmissionLineError(
            "the two modes must describe the same physical length, got "
            f"{odd.length_mm} and {even.length_mm} mm")

    differential = LineSpec(
        z0_ohm=2.0 * odd.z0_ohm, er_eff=odd.er_eff, length_mm=odd.length_mm,
        tan_delta=odd.tan_delta, r_ohm_per_m_at_hz=odd.r_ohm_per_m_at_hz)
    common = LineSpec(
        z0_ohm=0.5 * even.z0_ohm, er_eff=even.er_eff, length_mm=even.length_mm,
        tan_delta=even.tan_delta, r_ohm_per_m_at_hz=even.r_ohm_per_m_at_hz)

    diff_network = single_line_network(frequency, differential,
                                       z0_port=differential.z0_ohm)
    common_network = single_line_network(frequency, common,
                                         z0_port=common.z0_ohm)

    points = len(frequency.f)
    modal = np.zeros((points, 4, 4), dtype=complex)
    modal[:, 0:2, 0:2] = diff_network.s
    modal[:, 2:4, 2:4] = common_network.s

    modal_impedance = np.tile(
        np.array([differential.z0_ohm, differential.z0_ohm,
                  common.z0_ohm, common.z0_ohm], dtype=complex),
        (points, 1))

    single_ended_impedance = np.full((points, 4), float(z0_port), dtype=complex)
    network = skrf.Network(frequency=frequency, s=modal, z0=modal_impedance)
    # gmm2se's parameter is z0_se - the SINGLE-ENDED impedances to produce -
    # not the modal ones, which it already reads off the network. Passing the
    # modal set here raises rather than silently mis-normalising, which is how
    # this was caught.
    network.gmm2se(p=2, z0_se=single_ended_impedance)
    network.name = (f"pair_zdiff{2 * odd.z0_ohm:.1f}_"
                    f"{odd.length_mm:.3f}mm")
    return network


# --------------------------------------------------------------------------- #
# Port order
# --------------------------------------------------------------------------- #
PORT_ORDERS = ("per_end", "per_line")


def reorder_ports(network, source_order: str, target_order: str = "per_end"):
    """Convert a 4-port between the two Touchstone port conventions.

    ``per_end``  ports are [A_in, B_in, A_out, B_out] - what se2gmm expects.
    ``per_line`` ports are [A_in, A_out, B_in, B_out] - what many field
                 solvers and VNAs emit.

    Touchstone stores NO port-order metadata, so this cannot be detected from
    the file and the caller has to declare it. Getting it wrong does not
    produce a small error: the measurement above showed |Sdd21| going from 1
    to 0 on the same network.
    """
    for order in (source_order, target_order):
        if order not in PORT_ORDERS:
            raise TransmissionLineError(
                f"unknown port order {order!r}; expected one of {PORT_ORDERS}")
    if network.nports != 4:
        raise TransmissionLineError(
            f"port reordering is defined for 4-ports, got {network.nports}")
    if source_order == target_order:
        return network.copy()

    # per_line [A_in, A_out, B_in, B_out] -> per_end [A_in, B_in, A_out, B_out]
    permutation = [0, 2, 1, 3]          # self-inverse, so one map serves both
    reordered = network.copy()
    reordered.s = network.s[:, permutation][:, :, permutation]
    reordered.z0 = network.z0[:, permutation]
    return reordered


def mixed_mode(network, source_order: str = "per_end"):
    """Single-ended 4-port -> mixed-mode, with ports [D1, D2, C1, C2].

    The two-valued convenience form, kept because it reads well for the common
    case. ``to_mixed_mode`` is the general one: it takes the pairing as
    explicit port numbers and handles any number of pairs.
    """
    _require_skrf()
    per_end = reorder_ports(network, source_order, "per_end")
    converted = per_end.copy()
    converted.se2gmm(p=2)
    return converted


def to_mixed_mode(network, positives: Sequence[int] | None = None,
                  negatives: Sequence[int] | None = None):
    """Single-ended -> mixed-mode with the pairing given EXPLICITLY.

    ``positives``/``negatives`` are 1-BASED physical port numbers listed pair
    by pair: ``positives=[1, 3], negatives=[2, 4]`` means ports 1 and 2 are
    one pair and 3 and 4 the other. The ports are permuted into the
    interleaved order se2gmm expects (P1, N1, P2, N2, ...) before conversion.

    Passing neither argument assumes the ports are ALREADY interleaved - which
    is exactly the assumption that used to be made silently, so it is now
    something the caller opts into rather than inherits.
    """
    _require_skrf()
    if network.nports % 2 != 0:
        raise TransmissionLineError(
            f"mixed-mode needs an even port count, got {network.nports}: "
            "an odd port cannot belong to a pair")

    pair_count = network.nports // 2
    if positives is None and negatives is None:
        positives = [2 * k + 1 for k in range(pair_count)]
        negatives = [2 * k + 2 for k in range(pair_count)]
    if positives is None or negatives is None:
        raise TransmissionLineError(
            "give BOTH positives and negatives, or neither: a half-specified "
            "pairing is how the mode indices get mirrored")
    if len(positives) != pair_count or len(negatives) != pair_count:
        raise TransmissionLineError(
            f"expected {pair_count} positive and {pair_count} negative "
            f"port(s), got {len(positives)} and {len(negatives)}")

    ordered = [port for pair in zip(positives, negatives) for port in pair]
    if sorted(ordered) != list(range(1, network.nports + 1)):
        raise TransmissionLineError(
            f"positives+negatives must name each port exactly once, got {ordered}")

    work = network.copy()
    work.renumber([port - 1 for port in ordered], list(range(network.nports)))
    work.se2gmm(p=pair_count)
    return work


# Mixed-mode block indices produced by se2gmm.
DD, DC, CD, CC = 0, 1, 2, 3


def mixed_mode_db(mixed_network, block: int, out_pair: int = 1,
                  in_pair: int = 0) -> list[float]:
    """dB magnitude of one mixed-mode term across frequency.

    se2gmm groups the modes: ALL differential ports first, then all
    common-mode ports, so the block offset is ``pair_count`` and not 1.
    Hard-coding an offset of 1 works only for a 2-port-per-mode network and
    silently reads the wrong term for anything wider.

    Returns plain floats, not a numpy array: these values go straight into
    report violations, and ``report_io.dumps`` cannot serialise numpy scalars.
    """
    pair_count = mixed_network.nports // 2
    row_base = 0 if block in (DD, DC) else pair_count
    col_base = 0 if block in (DD, CD) else pair_count
    values = to_db(mixed_network.s[:, row_base + out_pair, col_base + in_pair])
    return [float(value) for value in values]


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
def cascade(networks: Sequence, allow_multiport: bool = False) -> object:
    """Chain networks in order: connector -> trace -> connector.

    2-ports only unless ``allow_multiport`` is set. skrf's ``**`` will happily
    cascade a 4-port under its OWN port-ordering assumption, and a silently
    mis-assumed ordering is precisely what this module exists to avoid: a
    multi-port channel should be assembled by the caller who knows which port
    meets which. Opting in makes that assumption visible at the call site.

    Every network must also share one frequency axis; skrf would interpolate
    silently, and a silently interpolated channel is a different channel.
    """
    _require_skrf()
    if not networks:
        raise TransmissionLineError("nothing to cascade")
    first = networks[0]
    if not allow_multiport:
        wrong = [index for index, network in enumerate(networks)
                 if network.nports != 2]
        if wrong:
            raise TransmissionLineError(
                f"cascade() takes 2-ports only; entries {wrong} are not. "
                "Assemble multi-port channels explicitly, or pass "
                "allow_multiport=True to accept skrf's port ordering")
    for index, network in enumerate(networks[1:], start=1):
        if len(network.f) != len(first.f) or not np.allclose(network.f, first.f):
            raise TransmissionLineError(
                f"network {index} has a different frequency grid; resample it "
                f"deliberately rather than letting the cascade interpolate")
        if network.nports != first.nports:
            raise TransmissionLineError(
                f"network {index} has {network.nports} ports, expected "
                f"{first.nports}")
    result = first
    for network in networks[1:]:
        result = result ** network
    return result


# --------------------------------------------------------------------------- #
# Touchstone I/O
# --------------------------------------------------------------------------- #
def read_touchstone(path):
    """Read a Touchstone file.

    A missing file raises FileNotFoundError, not a TransmissionLineError: the
    file being absent is an I/O fact, not a statement about the line, and the
    caller usually wants to handle the two differently.
    """
    from pathlib import Path as _Path
    _require_skrf()
    resolved = _Path(path)
    if not resolved.is_file():
        raise FileNotFoundError(f"Touchstone file not found: {resolved}")
    return skrf.Network(str(resolved))


def write_touchstone(network, path):
    """Write a Network as Touchstone, with the extension matching its ports.

    skrf picks the FORMAT from the file name, so a 4-port written to ``.s2p``
    produces a file that silently misparses on the next read. The extension is
    therefore DERIVED from ``network.nports`` rather than taken on trust.
    Returns the path actually written.
    """
    from pathlib import Path as _Path
    _require_skrf()
    resolved = _Path(path)
    expected = f".s{network.nports}p"
    if resolved.suffix.lower() != expected:
        resolved = resolved.with_suffix(expected)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    network.write_touchstone(str(resolved.with_suffix("")), form="ri")
    return resolved


# --------------------------------------------------------------------------- #
# Reporting helpers
# --------------------------------------------------------------------------- #
def to_db(values: np.ndarray) -> np.ndarray:
    """20*log10|S|, with zeros mapped to a finite floor.

    -inf is not valid JSON, and report_io dumps with allow_nan=False, so an
    ideal open would otherwise crash the report rather than describe it.
    """
    magnitude = np.abs(np.asarray(values))
    floored = np.maximum(magnitude, 1e-30)
    return 20.0 * np.log10(floored)


def worst_case(frequency_hz: np.ndarray, values_db: np.ndarray,
               kind: str = "min") -> dict[str, float]:
    """The extreme value and where it occurred."""
    array = np.asarray(values_db, dtype=float)
    if array.size == 0:
        raise TransmissionLineError("no samples to reduce")
    index = int(np.argmin(array)) if kind == "min" else int(np.argmax(array))
    return {
        "db": float(array[index]),
        "frequency_hz": float(np.asarray(frequency_hz)[index]),
    }


def insertion_loss_db(network, port_in: int = 0, port_out: int = 1
                      ) -> np.ndarray:
    return to_db(network.s[:, port_out, port_in])


def return_loss_db(network, port: int = 0) -> np.ndarray:
    return to_db(network.s[:, port, port])
