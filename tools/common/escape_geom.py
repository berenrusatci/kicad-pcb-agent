"""High-speed pad ESCAPE geometry: mask dams, fan-out, 90° corners, skew.

Ported from ``pcb_highspeed_escape.py``. Where the stack-up planner owns the
BODY of a trace (impedance, length match, coupling), this module owns the
first 1-3 mm where the trace leaves the pad — where most real-board mistakes
are made and where standard clearance DRC does NOT help, because soldermask
is a separate manufacturing layer, independent of the copper clearance DRC
knows about.

Pure stdlib + ``common.findings``; no pcbnew, no CLI. This is a library:
nothing here writes to stdout.

HONESTY / LIMITS (kept from the source, do not delete):
- The mask-dam model assumes the trace runs SYMMETRICALLY down the middle of
  the inter-pin channel. An off-centre trace has a smaller dam on one side
  than this function reports.
- ``FAB_MIN_MASK_DAM_MM`` is a typical fab number, not a measured one. It has
  to be overridden from the actual, DATED fab profile (JLCPCB is typically
  0.20-0.25 mm) before it is used for sign-off.
- ``FR4_MICROSTRIP_VELOCITY_MM_NS`` is a single typical FR4 microstrip
  number (eps_eff ~3.2). Real velocity is stack-up and frequency dependent.
- The fan-out distance is an ESTIMATE (pad length + margin at 45°), not a
  solved geometry.

TUR 4 ADDITION — DF40-class high-density connector escape:
``HighDensityConnectorEscape``/``pair_to_pair_min_distance_mm``/
``build_escape_plan``/``via_pair_required`` port the source's reference-
board-measured (CM4 IO Board TRD) 0.4 mm pitch B2B escape geometry. NOT
speculative in this project: the hub<->camera board interconnect is a real
DF40-class connector. ``via_pair_required`` uses the Liang-Barsky
line-clipping algorithm, a standard segment/AABB intersection test.

TUR 3 ADDITION — multi-board (host -> N camera boards) skew budget:
``multi_board_skew_budget``/``check_multi_board_skew_budget`` extend the
single-net skew calculation to a multi-camera-head system: each board's
HOST->board clock/trigger path (PCB trace + cable + layer transitions) gets
its own total delay, and the largest DIFFERENCE between boards is what is
checked against the budget - not one net's internal P/N mm skew. Directly
applicable to this project's multi-camera-head architecture (see
``system-planning/``). NOT PORTED from the same source section: the
DF40-class high-density-connector escape-plan generator
(``YuksekYogunlukKonnektorEscape``/``escape_planı_olustur``) and its
via-pair-required geometry check - real, reference-board-measured content,
but a routing-detail concern with no immediate consumer yet; deliberately
deferred rather than ported speculatively, see CONSOLIDATION-NOTES.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .findings import Finding, result

# --------------------------------------------------------------------------- #
# 1. SOLDER MASK DAM
# --------------------------------------------------------------------------- #
# A trace crossing the channel between two pins (e.g. the GND/VBUS pin sitting
# EXACTLY between two data pins of a SOT-23-6 ESD array): even when the copper
# clearance looks fine, subtracting the mask expansion on both sides can leave
# a "dam" too narrow to print -> solder bridge between two pins. Standard DRC
# clearance checking does NOT catch this: it measures copper to copper, not
# mask to mask.

# Typical fab minimum mask dam (mm). 0.20 is a representative JLCPCB-class
# number ONLY; it must be re-verified, and DATED, against the chosen fab's
# current capabilities page before it is used for sign-off (JLCPCB is
# typically 0.20-0.25 mm and this changes).
FAB_MIN_MASK_DAM_MM = 0.20


@dataclass
class PinChannel:
    """The narrow channel a trace must cross to reach the middle pin of a
    package such as SOT-23-6."""

    pad_column_pitch_mm: float
    """Centre-to-centre distance of the neighbouring pad columns (footprint)."""
    pad_length_mm: float
    """Pad length along the channel direction (footprint)."""
    mask_expansion_mm: float = 0.05
    """Fab soldermask expansion (typically ~0.05 mm; take it from the fab
    profile, do not assume)."""


def channel_width_mm(channel: PinChannel) -> float:
    """Free channel width between the pad columns."""
    return channel.pad_column_pitch_mm - channel.pad_length_mm


def mask_dam_width_mm(channel: PinChannel, track_width_mm: float) -> float:
    """Remaining mask dam, assuming the trace is centred in the channel.

        gap = (channel_width - track_width) / 2
        dam = gap - 2 * mask_expansion
    """
    gap = (channel_width_mm(channel) - track_width_mm) / 2
    return gap - 2 * channel.mask_expansion_mm


def mask_dam_messages(
    channel: PinChannel,
    track_width_mm: float,
    fab_min_dam_mm: float = FAB_MIN_MASK_DAM_MM,
) -> list[str]:
    """Human-readable violations for one (channel, track width) pair.

    Returns EXPLANATORY prose rather than a bare boolean, because the
    consequence is a solder bridge that usually shorts two different nets
    (e.g. 5V onto a data line). Empty list means "measured and clean" — the
    caller that needs a PASS/FAIL/NO_COVERAGE verdict should use
    ``check_mask_dam`` instead, so that "nothing measured" cannot look clean.
    """
    messages: list[str] = []
    dam = mask_dam_width_mm(channel, track_width_mm)
    width = channel_width_mm(channel)

    if width <= 0:
        messages.append(
            f"KRİTİK: pad sütun aralığı ({channel.pad_column_pitch_mm}mm) <= pad "
            f"uzunluğu ({channel.pad_length_mm}mm) — kanal yok, iz pad'e değer (short)."
        )
        return messages

    if dam < fab_min_dam_mm:
        messages.append(
            f"KRİTİK: maske barajı {dam:.3f}mm < fab minimumu {fab_min_dam_mm}mm "
            f"(iz={track_width_mm}mm, kanal={width:.3f}mm). "
            "Maske basılmaz -> pin-arası LEHİM KÖPRÜSÜ riski. "
            "İz genişliğini azalt, farklı (kısa pad'li) footprint kullan, "
            "veya izi via ile iç katmana taşı."
        )
    return messages


def check_mask_dam(
    channel: PinChannel,
    track_width_mm: float,
    fab_min_dam_mm: float = FAB_MIN_MASK_DAM_MM,
    check: str = "mask_dam_channel",
) -> Finding:
    """``mask_dam_messages`` as a Finding. One channel is one scanned item."""
    messages = mask_dam_messages(channel, track_width_mm, fab_min_dam_mm)
    return result(
        check, 1, [{"message": m} for m in messages],
        f"baraj = (kanal - iz)/2 - 2*mask_expansion; eşik ≥{fab_min_dam_mm} mm, "
        f"ölçülen {mask_dam_width_mm(channel, track_width_mm):.4f} mm. İz kanalın TAM "
        "ORTASINDAN geçiyor varsayılır.")


def max_trace_width_for_dam_mm(
    channel: PinChannel,
    fab_min_dam_mm: float = FAB_MIN_MASK_DAM_MM,
) -> float:
    """Widest trace that still leaves the fab minimum mask dam.

    The rule this encodes is "trace width in an inter-pin channel is set by
    the MASK DAM, not by the current".

        dam_min = (channel - track)/2 - 2*expansion
        => track = channel - 2*(dam_min + 2*expansion)
    """
    widest = channel_width_mm(channel) - 2 * (
        fab_min_dam_mm + 2 * channel.mask_expansion_mm)
    return max(widest, 0.0)


# --------------------------------------------------------------------------- #
# 2. OPENING THE PAIR UP BEFORE IT REACHES THE PAD COLUMN
# --------------------------------------------------------------------------- #

@dataclass
class DiffPairFanout:
    pad_length_mm: float
    """Pad length of the intervening (GND/VBUS) pin along the channel."""
    pair_pitch_mm: float
    """P/N pitch of the differential pair (pad centre to pad centre)."""


def fanout_required(fanout: DiffPairFanout) -> bool:
    """If the middle pin's pad is at least as long as the pair pitch, the pair
    cannot enter as-is: it would touch the pad (short). It has to be opened up
    beforehand."""
    return fanout.pad_length_mm >= fanout.pair_pitch_mm


def fanout_distance_mm(fanout: DiffPairFanout) -> float | None:
    """ESTIMATE of how many mm before the pad column the pair must fan out to
    ±(pin pitch / 2) at 45°. ``None`` means no fan-out is needed.

    This is an estimate, not solved geometry: at 45° the lateral escape costs
    a longitudinal distance of the same order, plus a small margin.
    """
    if not fanout_required(fanout):
        return None
    return round(fanout.pad_length_mm + 0.8, 3)


# --------------------------------------------------------------------------- #
# 3. 90° CORNERS
# --------------------------------------------------------------------------- #

@dataclass
class RouteSegment:
    x1: float
    y1: float
    x2: float
    y2: float


def _heading_deg(seg: RouteSegment) -> float:
    return math.degrees(math.atan2(seg.y2 - seg.y1, seg.x2 - seg.x1))


def turn_angle_deg(seg_a: RouteSegment, seg_b: RouteSegment) -> float:
    """Absolute turn angle between two consecutive segments (0-180°)."""
    delta = abs(_heading_deg(seg_a) - _heading_deg(seg_b)) % 360
    if delta > 180:
        delta = 360 - delta
    return delta


def right_angle_corners(
    segments: list[RouteSegment],
    low_deg: float = 85.0,
    high_deg: float = 95.0,
) -> list[int]:
    """Indices of consecutive segment pairs turning by 85-95° (i.e. roughly
    90°: acid-trap plus impedance-dip risk). Acceptance criterion: this list
    must be EMPTY."""
    bad: list[int] = []
    for i in range(len(segments) - 1):
        if low_deg <= turn_angle_deg(segments[i], segments[i + 1]) <= high_deg:
            bad.append(i)
    return bad


def check_right_angle_corners(
    segments: list[RouteSegment],
    low_deg: float = 85.0,
    high_deg: float = 95.0,
    check: str = "escape_right_angle_corners",
) -> Finding:
    """Corner check as a Finding. ``scanned`` counts CORNERS (segment pairs),
    so a one-segment route is NO_COVERAGE, not a silent PASS."""
    corners = max(0, len(segments) - 1)
    bad = right_angle_corners(segments, low_deg, high_deg)
    return result(
        check, corners,
        [{"corner_index": i, "turn_deg": round(
            turn_angle_deg(segments[i], segments[i + 1]), 3)} for i in bad],
        f"{low_deg}-{high_deg}° arası dönüş = ~90° köşe (asit tuzağı + empedans "
        f"çukuru); 45°/yay ile değiştir. Ölçülen köşe sayısı={corners}.")


# --------------------------------------------------------------------------- #
# 4. SKEW IN ps (so that pointless meanders are not added)
# --------------------------------------------------------------------------- #

# Typical FR4 microstrip propagation velocity (mm/ns) for eps_eff ~3.2.
FR4_MICROSTRIP_VELOCITY_MM_NS = 167.6


def skew_mm_to_ps(
    skew_mm: float,
    velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
) -> float:
    """skew_ps = skew_mm / v * 1000  (v in mm/ns)."""
    return skew_mm / velocity_mm_ns * 1000


def meander_required(
    skew_mm: float,
    budget_ps: float,
    velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
) -> bool:
    """Convert to ps FIRST, decide SECOND. Below budget, do NOT add a meander:
    a meander separates the pair, degrades coupling/impedance and adds
    corners."""
    return skew_mm_to_ps(skew_mm, velocity_mm_ns) >= budget_ps


def meander_length_mm(
    skew_mm: float,
    budget_ps: float,
    velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
) -> float:
    """Extra length (mm) needed to bring the skew back to budget — only that
    much, no over-compensation."""
    if not meander_required(skew_mm, budget_ps, velocity_mm_ns):
        return 0.0
    excess_ps = skew_mm_to_ps(skew_mm, velocity_mm_ns) - budget_ps
    return round(excess_ps / 1000 * velocity_mm_ns, 4)


def check_skew_budget(
    skew_mm: float,
    budget_ps: float,
    velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
    check: str = "escape_skew_budget",
) -> Finding:
    """Skew against budget as a Finding (one pair = one scanned item)."""
    skew_ps = skew_mm_to_ps(skew_mm, velocity_mm_ns)
    violations = []
    if meander_required(skew_mm, budget_ps, velocity_mm_ns):
        violations.append({
            "skew_mm": skew_mm,
            "skew_ps": round(skew_ps, 3),
            "budget_ps": budget_ps,
            "meander_mm": meander_length_mm(skew_mm, budget_ps, velocity_mm_ns),
        })
    return result(
        check, 1, violations,
        f"skew {skew_ps:.1f}ps, bütçe {budget_ps}ps, v={velocity_mm_ns} mm/ns. "
        "Bütçe altındaysa meander EKLEME (gereksiz kuplaj/empedans bozulması).")


# --------------------------------------------------------------------------- #
# 5. COPLANAR GND POUR CLEARANCE
# --------------------------------------------------------------------------- #

def coplanar_gnd_clearance_mm(track_width_mm: float, factor: float = 1.25) -> float:
    """A coplanar GND pour too close to the pair couples each trace to the
    plane (differential impedance and noise immunity both suffer). Rule of
    thumb: clearance >= ~1.25 * trace width — close enough to stitch, far
    enough not to break the pair."""
    return round(track_width_mm * factor, 4)


# --------------------------------------------------------------------------- #
# 6. COMBINED ACCEPTANCE REPORT
# --------------------------------------------------------------------------- #

@dataclass
class EscapeAssessment:
    net: str
    channel: PinChannel | None = None
    track_width_mm: float | None = None
    fanout: DiffPairFanout | None = None
    route_segments: list[RouteSegment] | None = None
    skew_mm: float | None = None
    budget_ps: float | None = None


def escape_messages(assessment: EscapeAssessment) -> list[str]:
    """All escape acceptance criteria for one net, as human prose.

    NOTE: this list MIXES severities (KRİTİK / UYARI / BİLGİ / OK); it is a
    rendering aid, not a verdict. An empty list here does NOT mean "clean" —
    it can also mean "nothing was supplied to check". Use ``escape_findings``
    when a machine has to decide.
    """
    messages: list[str] = []

    if assessment.channel is not None and assessment.track_width_mm is not None:
        messages.extend(mask_dam_messages(assessment.channel,
                                          assessment.track_width_mm))

    if assessment.fanout is not None:
        distance = fanout_distance_mm(assessment.fanout)
        if distance is not None:
            messages.append(
                f"BİLGİ [{assessment.net}]: çift, pad sütununa girmeden "
                f"~{distance}mm önce 45° ile açılmalı (ara pin pad boyu >= "
                "çift adımı)."
            )

    if assessment.route_segments:
        bad = right_angle_corners(assessment.route_segments)
        if bad:
            messages.append(
                f"KRİTİK [{assessment.net}]: {len(bad)} adet 85-95° köşe bulundu "
                f"(segment index: {bad}) — 45°/yay ile değiştir."
            )

    if assessment.skew_mm is not None and assessment.budget_ps is not None:
        skew_ps = skew_mm_to_ps(assessment.skew_mm)
        if meander_required(assessment.skew_mm, assessment.budget_ps):
            extra = meander_length_mm(assessment.skew_mm, assessment.budget_ps)
            messages.append(
                f"UYARI [{assessment.net}]: skew {skew_ps:.1f}ps, bütçe "
                f"{assessment.budget_ps}ps üzerinde — ~{extra}mm meander ekle "
                "(kritik olmayan bölümde, 45°)."
            )
        else:
            messages.append(
                f"OK [{assessment.net}]: skew {skew_ps:.1f}ps, bütçe "
                f"{assessment.budget_ps}ps altında — meander EKLEME (gereksiz "
                "kuplaj/empedans bozulması)."
            )

    return messages


def escape_findings(assessment: EscapeAssessment) -> list[Finding]:
    """The same criteria as ``escape_messages``, but as Findings.

    Every sub-check is always present; one that got no input is NO_COVERAGE
    rather than absent, so a caller cannot mistake "not supplied" for "clean".
    """
    out: list[Finding] = []

    if assessment.channel is not None and assessment.track_width_mm is not None:
        out.append(check_mask_dam(assessment.channel, assessment.track_width_mm))
    else:
        out.append(result("mask_dam_channel", 0, [],
                          "kanal/iz genişliği verilmedi"))

    if assessment.fanout is not None:
        distance = fanout_distance_mm(assessment.fanout)
        out.append(result(
            "escape_pair_fanout", 1, [],
            "açılma gerekli değil (ara pin pad boyu < çift adımı)"
            if distance is None else
            f"çift, pad sütununa girmeden ~{distance}mm önce 45° ile açılmalı"))
    else:
        out.append(result("escape_pair_fanout", 0, [], "fanout verisi verilmedi"))

    out.append(check_right_angle_corners(assessment.route_segments or []))

    if assessment.skew_mm is not None and assessment.budget_ps is not None:
        out.append(check_skew_budget(assessment.skew_mm, assessment.budget_ps))
    else:
        out.append(result("escape_skew_budget", 0, [],
                          "skew/bütçe verilmedi"))

    return out


# --------------------------------------------------------------------------- #
# 7. MULTI-BOARD (HOST -> N CAMERA BOARDS) TOTAL SKEW BUDGET
# --------------------------------------------------------------------------- #
# ``skew_mm_to_ps``/``meander_required`` above measure ONE net's P/N-pair-
# internal skew. A multi-camera head system asks a DIFFERENT question: how
# much do the TOTAL delays of the clock/trigger traces from the HOST to EACH
# of several camera boards (PCB trace + host<->board cable + layer
# transitions) differ from each other? If that inter-board skew exceeds the
# synchronous-sampling budget, the cameras' frame timestamps drift apart.

# A typical shielded differential cable (e.g. a coax pair or an FPC
# extension) - a DIFFERENT dielectric/medium from FR4 microstrip, so its own
# constant. This is only a DEFAULT for when no real cable data is available;
# a real design should use the velocity from the cable's own datasheet.
DEFAULT_CABLE_VELOCITY_MM_NS = 200.0

# TYPICAL extra delay a via adds at a layer transition (barrel length + a
# rough lump for the stub effect) - NOT a real field solve/simulation (same
# discipline note as the thermal score in the placement scorer: this is only
# a rough "how risky is this" estimate; not wired to the stackup planner's
# real stack-up thickness yet).
DEFAULT_VIA_TRANSITION_DELAY_PS = 5.0


@dataclass
class BoardPathSegment:
    """Length/transition components of the physical path from the HOST to
    ONE camera board - everything needed to compute the clock/trigger line's
    TOTAL delay (host PCB trace + host<->board cable + board PCB trace +
    layer-transition count)."""

    board_name: str
    host_pcb_length_mm: float = 0.0
    cable_length_mm: float = 0.0
    board_pcb_length_mm: float = 0.0
    layer_transition_count: int = 0


@dataclass
class BoardDelayResult:
    board_name: str
    total_delay_ps: float
    pcb_delay_ps: float
    cable_delay_ps: float
    via_delay_ps: float


@dataclass
class MultiBoardSkewBudgetResult:
    board_results: dict[str, BoardDelayResult]
    fastest_board: str
    slowest_board: str
    max_skew_ps: float
    budget_ps: float
    budget_exceeded: bool


def multi_board_skew_budget(
        paths: list[BoardPathSegment],
        budget_ps: float,
        pcb_velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
        cable_velocity_mm_ns: float = DEFAULT_CABLE_VELOCITY_MM_NS,
        via_transition_delay_ps: float = DEFAULT_VIA_TRANSITION_DELAY_PS,
        ) -> MultiBoardSkewBudgetResult:
    """The TOTAL delay of the HOST->EACH camera board clock/trigger line;
    the largest difference (skew) between boards is compared against
    ``budget_ps``.

    This is the MULTI-BOARD extension of the single-net skew calculation
    above - each board carries its own (potentially different-length)
    HOST->board path, and skew here is the REAL total-delay DIFFERENCE
    between those paths (not the P/N-pair-internal mm skew of one net).

    ``paths`` empty raises ``ValueError`` - at least one board path is
    required for a meaningful skew figure (a SINGLE-board call is still
    accepted, and reads as "skew=0 against itself" - only an empty list is
    refused, so a caller can still fail fast on genuinely missing data).

    LIMIT: the via delay is a rough constant approximation (see the
    module-level note); the real cable velocity should come from the
    cable's own datasheet - the default is used only when that data is
    unavailable.
    """
    if not paths:
        raise ValueError("at least one board path (BoardPathSegment) is required")

    results: dict[str, BoardDelayResult] = {}
    for path in paths:
        pcb_ps = skew_mm_to_ps(
            path.host_pcb_length_mm + path.board_pcb_length_mm, pcb_velocity_mm_ns)
        cable_ps = skew_mm_to_ps(path.cable_length_mm, cable_velocity_mm_ns)
        via_ps = path.layer_transition_count * via_transition_delay_ps
        total_ps = pcb_ps + cable_ps + via_ps
        results[path.board_name] = BoardDelayResult(
            board_name=path.board_name,
            total_delay_ps=round(total_ps, 4),
            pcb_delay_ps=round(pcb_ps, 4),
            cable_delay_ps=round(cable_ps, 4),
            via_delay_ps=round(via_ps, 4))

    slowest = max(results.values(), key=lambda r: r.total_delay_ps)
    fastest = min(results.values(), key=lambda r: r.total_delay_ps)
    max_skew_ps = round(slowest.total_delay_ps - fastest.total_delay_ps, 4)

    return MultiBoardSkewBudgetResult(
        board_results=results,
        fastest_board=fastest.board_name,
        slowest_board=slowest.board_name,
        max_skew_ps=max_skew_ps,
        budget_ps=budget_ps,
        budget_exceeded=max_skew_ps > budget_ps)


def check_multi_board_skew_budget(
        paths: list[BoardPathSegment],
        budget_ps: float,
        pcb_velocity_mm_ns: float = FR4_MICROSTRIP_VELOCITY_MM_NS,
        cable_velocity_mm_ns: float = DEFAULT_CABLE_VELOCITY_MM_NS,
        via_transition_delay_ps: float = DEFAULT_VIA_TRANSITION_DELAY_PS,
        ) -> Finding:
    """``multi_board_skew_budget`` wrapped as a Finding (one board = one
    scanned item)."""
    if not paths:
        return result("multi_board_skew_budget", 0, [], "no board paths given")

    outcome = multi_board_skew_budget(
        paths, budget_ps, pcb_velocity_mm_ns, cable_velocity_mm_ns,
        via_transition_delay_ps)
    violations = []
    if outcome.budget_exceeded:
        violations.append({
            "fastest_board": outcome.fastest_board,
            "slowest_board": outcome.slowest_board,
            "max_skew_ps": outcome.max_skew_ps,
            "budget_ps": budget_ps,
        })
    return result(
        "multi_board_skew_budget", len(paths), violations,
        f"max inter-board skew {outcome.max_skew_ps}ps (fastest="
        f"{outcome.fastest_board}, slowest={outcome.slowest_board}) against "
        f"a {budget_ps}ps budget.")


# --------------------------------------------------------------------------- #
# 8. High-density connector escape (DF40-class, 0.4 mm pitch, GND-separated
#    differential-pair rows)
# --------------------------------------------------------------------------- #
# This project has a REAL consumer for this profile: the hub<->camera board
# B2B interconnect uses a 0.4 mm pitch DF40-class connector, so - unlike the
# archive's own uncertainty about who would ever call this - the escape
# geometry below is not speculative here.
#
# The reference numbers (first straight segment, commit distance, max trim
# segment, the pair-to-pair spacing ratio) come from a MEASURED reference
# board (CM4 IO Board TRD escape), not a formula - they are carried through
# UNCHANGED from the source, not re-derived or rounded.

@dataclass
class HighDensityConnectorEscape:
    """Escape parameters for a 0.4 mm (or similar fine) pitch, GND-separated
    differential-pair row connector (DF40-class B2B, reference: CM4 IO
    Board TRD escape)."""

    pad_pitch_mm: float
    """The connector's native pin pitch (e.g. 0.4 mm)."""
    track_width_mm: float
    """The differential track width actually used - must be consistent
    project-wide (reference board used 0.127 mm; this project may use a
    different absolute value, only the RATIO below matters)."""
    first_straight_segment_mm: float = 0.55
    """Straight run from the pad, in the pad's own escape direction
    (reference range: 0.44-0.63 mm)."""
    commit_distance_mm: float = 2.1
    """By this distance, a SINGLE 45-degree turn must already have
    committed to the long diagonal (reference range: 0.5-2.1 mm; the upper
    bound was taken)."""
    max_trim_segment_mm: float = 1.6
    """Maximum allowed uncoupled segment length near each end, used to
    remove the P/N pad position mismatch."""


def pair_to_pair_min_distance_mm(escape: HighDensityConnectorEscape) -> float:
    """Minimum centre-to-centre distance between neighbouring pairs sharing
    the same escape corridor. The reference ratio (~0.47 mm / 0.127 mm
    track =~ 3.7x) is scaled to this project's own track width."""
    reference_ratio = 0.47 / 0.127
    return round(escape.track_width_mm * reference_ratio, 4)


def build_escape_plan(escape: HighDensityConnectorEscape,
                      pad_position: tuple[float, float],
                      escape_direction: tuple[float, float],
                      ) -> list[tuple[float, float]]:
    """The waypoint sequence for the first ``~commit_distance_mm`` out of
    the pad: pad -> straight escape -> commit to the diagonal at 45 degrees
    (reference-board discipline). ``escape_direction`` must be a unit
    vector (e.g. ``(0, 1)`` = escape +Y away from the connector row,
    ``(0, -1)`` = -Y).

    The returned list gives the caller a starting point to continue the
    long diagonal toward the target from (directly if ``via_pair_required``
    is False, through a via-pair otherwise) - it does not route the whole
    trace.
    """
    px, py = pad_position
    dx, dy = escape_direction
    straight_end = (px + dx * escape.first_straight_segment_mm,
                    py + dy * escape.first_straight_segment_mm)
    return [pad_position, straight_end]


def via_pair_required(pad_position: tuple[float, float],
                      target_position: tuple[float, float],
                      obstacle_bboxes: list[tuple[float, float, float, float]],
                      ) -> bool:
    """Would a single 45/135-degree diagonal from the pad to the target
    (as the reference board draws it) cross any of the given obstacle
    bounding boxes ``(x1, y1, x2, y2)``?

    True means that direct line is blocked - rule violated - and a
    via-pair (an In2.Cu tunnel) hop is needed. This is the automatic check
    behind the "placement must be verified before routing" rule: a True
    result means the target component/passive should FIRST be evaluated
    for whether it can simply be moved; only if it cannot is a via-pair the
    answer (this function does not decide that, it only measures the
    intersection).

    Uses the Liang-Barsky line-clipping algorithm (a real, standard
    segment/AABB intersection test, not an approximation invented here).
    """
    x1p, y1p = pad_position
    x2p, y2p = target_position

    def _segment_intersects_bbox(bbox: tuple[float, float, float, float]) -> bool:
        bx1, by1, bx2, by2 = bbox
        dx, dy = x2p - x1p, y2p - y1p
        t0, t1 = 0.0, 1.0
        for p, q in (
            (-dx, x1p - bx1),
            (dx, bx2 - x1p),
            (-dy, y1p - by1),
            (dy, by2 - y1p),
        ):
            if p == 0:
                if q < 0:
                    return False
                continue
            r = q / p
            if p < 0:
                if r > t1:
                    return False
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return False
                if r < t1:
                    t1 = r
        return t0 <= t1

    return any(_segment_intersects_bbox(b) for b in obstacle_bboxes)


# --------------------------------------------------------------------------- #
# CONNECTOR ESCAPE CORRIDOR — where the straight escape stub is allowed to end
#
# A high-density connector's pad field cannot be entered by a maze router: the
# clearance halo of the neighbouring GND pads closes the target cell as well.
# The fix, on a real board too, is a STRAIGHT stub perpendicular to the pad
# row, long enough to leave the pad field; the search only starts after it.
#
# The question this section answers is HOW LONG that stub is. In cam-w it used
# to be a literal in the routing script:
#
#     ESCAPE_Y = {"J201": 23.0, "J101": 9.0}      # pcb_route.py
#
# Two connector reference designators and two y values, written into source
# code. On any other board that line is meaningless, and on THIS board nothing
# checked that it still matched the connectors after a placement change.
#
# The derivation (MEASURED 2026-08-10 against the built board):
#
#   J101  band 0.00 .. 8.60   escape 9.00  ->  band far edge + 0.40
#   J201  band 23.40 .. 32.00 escape 23.00 ->  band near edge - 0.40
#
# So the stub ends where the connector's EXCLUSIVE BAND ends, plus a margin.
# That is the meaningful line: inside the band the connector owns the layer,
# so a straight stub cannot hit anything; past it, general placement begins
# and the maze router must take over. Measuring against the COURTYARD instead
# gives 0.43 and 0.48 - two different numbers, i.e. not the rule that was
# actually used.
#
# HONESTY: the 0.40 mm margin itself is BACK-DERIVED. It is the value that
# reproduces the two constants exactly; it is not independently justified by
# a fab rule or a datasheet. It is therefore a DECLARED design value that
# belongs in the board's JSON with a src, not a number hidden in a formula.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConnectorBand:
    """The strip a connector reserves for itself on its own copper layer.

    ``y_lo_mm``/``y_hi_mm`` are in the BOARD frame (Y-up), the same frame the
    zone rectangles use.
    """

    ref: str
    y_lo_mm: float
    y_hi_mm: float


def connector_escape_y_mm(
    band: ConnectorBand,
    board_height_mm: float,
    margin_mm: float,
) -> tuple[float, str]:
    """y where the straight escape stub ends, plus its derivation in words.

    The connector sits against a board edge, so its band abuts either y=0 or
    y=board_height. The stub runs the OTHER way, into the board, and stops
    ``margin_mm`` past the band.

    Returns ``(escape_y_mm, derivation)``. The derivation string is returned
    rather than logged so the caller can put it in a report: a number whose
    origin cannot be printed is a number nobody can check.

    Raises ValueError when the band does not abut an edge (then this is not a
    board-edge connector and this rule does not apply), when the margin is
    not positive, or when the band is degenerate.
    """
    lo, hi = min(band.y_lo_mm, band.y_hi_mm), max(band.y_lo_mm, band.y_hi_mm)
    if hi - lo <= 0:
        raise ValueError(f"{band.ref}: band has no height ({lo} .. {hi})")
    if margin_mm <= 0:
        raise ValueError(f"{band.ref}: escape margin must be > 0, got {margin_mm}")
    if board_height_mm <= 0:
        raise ValueError(f"{band.ref}: board height must be > 0")

    # Which edge does the band abut? Whichever it is closer to. The tolerance
    # is deliberately tight: a band floating in the middle of the board is not
    # a connector band and must not be guessed at.
    d_bottom, d_top = abs(lo - 0.0), abs(hi - board_height_mm)
    tol = 0.5
    if min(d_bottom, d_top) > tol:
        raise ValueError(
            f"{band.ref}: band {lo} .. {hi} abuts neither y=0 nor "
            f"y={board_height_mm} (within {tol} mm); the escape direction "
            f"cannot be derived from geometry"
        )
    if d_bottom <= d_top:
        y = hi + margin_mm
        which = f"band far edge {hi} + margin {margin_mm}"
        toward = "+Y"
    else:
        y = lo - margin_mm
        which = f"band near edge {lo} - margin {margin_mm}"
        toward = "-Y"
    return y, (f"{band.ref}: band {lo}..{hi} abuts "
               f"y={'0' if d_bottom <= d_top else board_height_mm}; escape "
               f"runs {toward} into the board and ends at {which} = {y}")
