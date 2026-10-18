"""The I²C pull-up window, and the provenance every number in it carries.

WHY THIS IS NOT A ONE-LINE FORMULA
--------------------------------------------------------------------------
    Rmin = (VDD - VOL) / IOL        the driver must be able to hold the line low
    Rmax = tr / (k * C_bus)         the rail must be able to pull it high in time

Both lines are trivial arithmetic, and the arithmetic is not the hard part.
The hard part is the five numbers they consume, because each one is a number
a tool could invent and an engineer would then believe:

  VDD     the rail the pull-ups actually return to, which is not always the
          rail on the block diagram
  VOL     the WEAKEST driver's output-low voltage - a bus is only as strong
          as the device that has to sink the whole pull-up current
  IOL     the current VOL is specified AT. VOL without its IOL is not a
          datasheet figure, it is half of one
  C_bus   trace + pin + connector + cable. Nothing in a netlist knows this
  tr      the selected mode's rise-time limit, which does not exist except
          relative to a spec document, its revision, and a clause in it

So this module holds NO I²C number. Standard-mode, Fast-mode and Fast-mode
Plus differ here only in the inputs the caller supplies; a tool that shipped
"300 ns" as a constant would be asserting a spec revision it never read.

WHERE 0.85 WENT
--------------------------------------------------------------------------
The commonly written ``Rmax ~= tr / (0.85 * C_bus)`` is not a physical
constant. It is ``ln((1 - f_lo) / (1 - f_hi))`` for the fractions of VDD the
spec measures the rise time BETWEEN - 0.3 and 0.7 give 0.8473, which rounds
to the 0.85 in the note. That band is a spec statement, so the caller
declares the band (with its source) and :func:`rise_time_coefficient` does
the log. A caller whose source states the coefficient directly may declare
that instead; the report records which of the two happened.

EVERY INPUT IS SOURCED OR IT IS NOT AN INPUT
--------------------------------------------------------------------------
:func:`read_sourced` accepts ``{"value": ..., "source": {"document": ...,
"revision": ...}}`` and nothing else. A bare number is not an error, it is a
GAP - the caller turns gaps into NO_COVERAGE. The distinction matters:
"you gave me 3.3 with no provenance" must never round to "3.3".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# Gap codes - the reasons a run is NO_COVERAGE rather than PASS or FAIL
# --------------------------------------------------------------------------- #
MISSING_INPUT = "MISSING_INPUT"
UNSOURCED_INPUT = "UNSOURCED_INPUT"
SOURCE_INCOMPLETE = "SOURCE_INCOMPLETE"
MISSING_MODE = "MISSING_MODE"
SEGMENT_NET_NOT_IN_NETLIST = "SEGMENT_NET_NOT_IN_NETLIST"
RAIL_NET_NOT_IN_NETLIST = "RAIL_NET_NOT_IN_NETLIST"
COMPONENT_NOT_IN_NETLIST = "COMPONENT_NOT_IN_NETLIST"
UNPARSEABLE_RESISTOR_VALUE = "UNPARSEABLE_RESISTOR_VALUE"

# The fields a source block must carry. ``clause`` is carried through when
# given but is not required: some sources (a bench measurement, a one-page
# app note) have no clause to cite, and demanding one would push honest
# inputs into NO_COVERAGE.
REQUIRED_SOURCE_FIELDS = ("document", "revision")


@dataclass(frozen=True)
class Gap:
    """One reason the answer is "not established" rather than a verdict."""

    field: str
    code: str
    detail: str

    def to_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Source:
    """Where a number came from, in enough detail to go and look."""

    document: str
    revision: str
    clause: Optional[str] = None

    def cite(self) -> str:
        base = f"{self.document} rev {self.revision}"
        return f"{base} §{self.clause}" if self.clause else base

    def to_dict(self) -> dict:
        out = {"document": self.document, "revision": self.revision}
        if self.clause:
            out["clause"] = self.clause
        return out


@dataclass(frozen=True)
class Sourced:
    """A number that knows where it came from."""

    field: str
    value: float
    unit: str
    source: Source
    method: Optional[str] = None

    def to_dict(self) -> dict:
        out = {"value": self.value, "unit": self.unit,
               "source": self.source.to_dict(), "cite": self.source.cite()}
        if self.method:
            out["method"] = self.method
        return out


def read_source(block, where: str) -> Tuple[Optional[Source], Optional[Gap]]:
    """Validate one ``source`` block. Returns ``(source, None)`` or ``(None, gap)``."""
    if block is None:
        return None, Gap(where, UNSOURCED_INPUT,
                         "no 'source' block: a number with no provenance "
                         "cannot be audited and is not an input")
    if not isinstance(block, Mapping):
        raise ValueError(f"{where}: 'source' must be an object")
    missing = [name for name in REQUIRED_SOURCE_FIELDS
               if not str(block.get(name, "")).strip()]
    if missing:
        return None, Gap(where, SOURCE_INCOMPLETE,
                         f"source is missing {', '.join(missing)}; "
                         "a document without a revision names a moving target")
    clause = block.get("clause")
    return Source(document=str(block["document"]).strip(),
                  revision=str(block["revision"]).strip(),
                  clause=str(clause).strip() if clause else None), None


def read_sourced(container, field: str, unit: str
                 ) -> Tuple[Optional[Sourced], Optional[Gap]]:
    """Read one sourced scalar out of an ``inputs`` object.

    A bare number (``"vdd_v": 3.3``) is a GAP, not an error and certainly not
    a value: it is exactly the shape a hurried operator produces, and reading
    it would put an unauditable number into a PASS.
    """
    if not isinstance(container, Mapping):
        raise ValueError(f"{field}: 'inputs' must be an object")
    entry = container.get(field)
    if entry is None:
        return None, Gap(field, MISSING_INPUT, f"{field} was not declared")
    if not isinstance(entry, Mapping):
        return None, Gap(field, UNSOURCED_INPUT,
                         f"{field} was given as a bare value; declare it as "
                         '{"value": ..., "source": {"document": ..., '
                         '"revision": ...}}')
    if "value" not in entry or entry["value"] is None:
        return None, Gap(field, MISSING_INPUT, f"{field} has no 'value'")
    raw = entry["value"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{field}: 'value' must be a number, got {raw!r}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{field}: 'value' must be finite, got {raw!r}")
    source, gap = read_source(entry.get("source"), field)
    if gap is not None:
        return None, gap
    method = entry.get("method")
    return Sourced(field=field, value=value, unit=unit, source=source,
                   method=str(method) if method else None), None


@dataclass(frozen=True)
class RiseTimeBand:
    """The two fractions of VDD the spec measures the rise time between."""

    low_fraction: float
    high_fraction: float
    source: Source

    def to_dict(self) -> dict:
        return {"low_fraction": self.low_fraction,
                "high_fraction": self.high_fraction,
                "source": self.source.to_dict(),
                "cite": self.source.cite()}


def read_rise_time_band(container, field: str = "rise_time_band"
                        ) -> Tuple[Optional[RiseTimeBand], Optional[Gap]]:
    """Read the declared measurement band, source and all."""
    if not isinstance(container, Mapping):
        raise ValueError(f"{field}: 'inputs' must be an object")
    entry = container.get(field)
    if entry is None:
        return None, Gap(field, MISSING_INPUT, f"{field} was not declared")
    if not isinstance(entry, Mapping):
        raise ValueError(f"{field}: must be an object with 'low_fraction' "
                         "and 'high_fraction'")
    for name in ("low_fraction", "high_fraction"):
        if entry.get(name) is None:
            return None, Gap(field, MISSING_INPUT, f"{field}.{name} is absent")
        if isinstance(entry[name], bool) or not isinstance(entry[name], (int, float)):
            raise ValueError(f"{field}.{name}: must be a number")
    low = float(entry["low_fraction"])
    high = float(entry["high_fraction"])
    source, gap = read_source(entry.get("source"), field)
    if gap is not None:
        return None, gap
    return RiseTimeBand(low, high, source), None


# --------------------------------------------------------------------------- #
# The arithmetic. No I²C number appears below - only the caller's.
# --------------------------------------------------------------------------- #
def r_min_ohms(vdd_v: float, vol_v: float, iol_a: float) -> float:
    """Smallest pull-up the weakest driver can still hold at VOL.

    Below this the driver is asked for more than IOL and the low level rises
    above VOL - the bus does not fail cleanly, it fails at a temperature.
    """
    if iol_a <= 0:
        raise ValueError(f"IOL must be positive, got {iol_a!r}")
    if vdd_v <= 0:
        raise ValueError(f"VDD must be positive, got {vdd_v!r}")
    if vol_v < 0:
        raise ValueError(f"VOL must not be negative, got {vol_v!r}")
    if vol_v >= vdd_v:
        raise ValueError(
            f"VOL ({vol_v}) is not below VDD ({vdd_v}); one of the two is "
            "quoted against the wrong rail, and the difference is the whole "
            "numerator")
    return (vdd_v - vol_v) / iol_a


def rise_time_coefficient(low_fraction: float, high_fraction: float) -> float:
    """RC multiplier implied by the band the spec measures tr between.

    ``ln((1 - f_lo) / (1 - f_hi))``. For the 0.3/0.7 band this is 0.8473,
    which is where the "0.85" in the shorthand comes from. Deriving it keeps
    the number attributable to the band the caller declared instead of to a
    constant nobody can cite.
    """
    for name, value in (("low_fraction", low_fraction),
                        ("high_fraction", high_fraction)):
        if not 0.0 < value < 1.0:
            raise ValueError(f"{name} must lie strictly between 0 and 1, "
                             f"got {value!r}")
    if low_fraction >= high_fraction:
        raise ValueError(
            f"low_fraction ({low_fraction}) must be below high_fraction "
            f"({high_fraction}); a rise time is measured upwards")
    return math.log((1.0 - low_fraction) / (1.0 - high_fraction))


def r_max_ohms(t_rise_s: float, c_bus_f: float, coefficient: float) -> float:
    """Largest pull-up whose RC still meets the mode's rise-time limit."""
    if t_rise_s <= 0:
        raise ValueError(f"rise time must be positive, got {t_rise_s!r}")
    if c_bus_f <= 0:
        raise ValueError(f"bus capacitance must be positive, got {c_bus_f!r}")
    if coefficient <= 0:
        raise ValueError(f"coefficient must be positive, got {coefficient!r}")
    return t_rise_s / (coefficient * c_bus_f)


def parallel_ohms(values: Sequence[float]) -> Optional[float]:
    """Equivalent of pull-ups sitting on the SAME segment.

    This is the arithmetic the row exists for. Two boards that each carry a
    correct 4.7k pull-up, wired together over a connector, present 2.35k -
    and neither schematic is wrong on its own, which is why nobody notices.
    ``None`` for an empty list: no pull-up at all is not "infinite ohms, near
    enough", it is a distinct condition the caller must report by name.
    """
    if not values:
        return None
    for value in values:
        if value <= 0:
            raise ValueError(f"a pull-up of {value!r} ohms is not a pull-up")
    return 1.0 / sum(1.0 / value for value in values)


IN_WINDOW = "IN_WINDOW"
BELOW_RMIN = "BELOW_RMIN"
ABOVE_RMAX = "ABOVE_RMAX"
WINDOW_EMPTY = "WINDOW_EMPTY"
NO_PULLUP = "NO_PULLUP"


def classify(parallel: Optional[float], r_min: float, r_max: float) -> str:
    """Where the resolved network sits relative to the window."""
    if r_min > r_max:
        # Checked first: no resistor satisfies it, so blaming the fitted one
        # would point the reader at the wrong component. The fix is C_bus,
        # the mode, or the driver - never the resistor.
        return WINDOW_EMPTY
    if parallel is None:
        return NO_PULLUP
    if parallel < r_min:
        return BELOW_RMIN
    if parallel > r_max:
        return ABOVE_RMAX
    return IN_WINDOW


VERDICT_DETAIL = {
    IN_WINDOW: "parallel equivalent lies inside [Rmin, Rmax]",
    BELOW_RMIN: "parallel equivalent is below Rmin: the driver is asked for "
                "more than its specified IOL and VOL is not guaranteed",
    ABOVE_RMAX: "parallel equivalent is above Rmax: the rise time exceeds "
                "the declared limit for this mode",
    WINDOW_EMPTY: "Rmin exceeds Rmax: no resistor satisfies this mode at "
                  "this bus capacitance - change C_bus, the mode, or the driver",
    NO_PULLUP: "no pull-up bridges this net to the declared rail",
}


# --------------------------------------------------------------------------- #
# Resistor values as written in a netlist
# --------------------------------------------------------------------------- #
_MULTIPLIER = {"R": 1.0, "r": 1.0, "E": 1.0, "e": 1.0,
               "K": 1e3, "k": 1e3, "M": 1e6, "m": 1e-3, "G": 1e9}
_TRAILING_UNIT = re.compile(r"(?:ohms?|Ω|Ω)$", re.IGNORECASE)
_SUFFIXED = re.compile(r"^([0-9]*\.?[0-9]+)\s*([RrEeKkMmG]?)$")
_INFIXED = re.compile(r"^([0-9]+)([RrEeKkMmG])([0-9]+)$")


def parse_resistance(text) -> float:
    """``"2.2k"`` / ``"2k2"`` / ``"470R"`` / ``"470"`` -> ohms.

    Deliberately strict. ``"DNP"``, ``"4.7k 1%"`` and an empty value raise,
    because the caller's only honest response to a value it cannot read is
    NO_COVERAGE - a pull-up network with one unknown member has an unknown
    parallel equivalent, and rounding that to "the ones I could read" biases
    the answer high, which is the direction that passes.
    """
    if text is None:
        raise ValueError("no value")
    raw = str(text).strip()
    if not raw:
        raise ValueError("empty value")
    raw = _TRAILING_UNIT.sub("", raw).strip()
    match = _SUFFIXED.match(raw)
    if match:
        digits, suffix = match.groups()
        return float(digits) * (_MULTIPLIER[suffix] if suffix else 1.0)
    match = _INFIXED.match(raw)
    if match:
        whole, suffix, fraction = match.groups()
        return float(f"{whole}.{fraction}") * _MULTIPLIER[suffix]
    raise ValueError(f"{text!r} is not a resistance this reader understands")


# --------------------------------------------------------------------------- #
# Resolving the pull-up network from the netlist, not from an assumption
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PullUp:
    """One two-terminal part bridging the segment net and the rail net."""

    ref: str
    value_text: str
    ohms: float
    line_pin: str
    rail_pin: str

    def to_dict(self) -> dict:
        return {"ref": self.ref, "value": self.value_text, "ohms": self.ohms,
                "line_pin": self.line_pin, "rail_pin": self.rail_pin}


@dataclass
class Resolution:
    """What the netlist actually says about one line of one segment."""

    line: str
    net: str
    rail_net: str
    pullups: List[PullUp] = dataclass_field(default_factory=list)
    parallel_ohms: Optional[float] = None
    refs_on_net: List[str] = dataclass_field(default_factory=list)
    other_bridging: List[Tuple[str, int]] = dataclass_field(default_factory=list)
    gaps: List[Gap] = dataclass_field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not self.gaps

    def to_dict(self) -> dict:
        return {
            "line": self.line,
            "net": self.net,
            "rail_net": self.rail_net,
            "pullups": [p.to_dict() for p in self.pullups],
            "parallel_ohms": self.parallel_ohms,
            "refs_on_net": self.refs_on_net,
            "other_bridging_refs": [{"ref": ref, "pins": pins}
                                    for ref, pins in self.other_bridging],
            "gaps": [g.to_dict() for g in self.gaps],
        }


def pins_by_ref(nets: Mapping[str, Sequence[Tuple[str, str]]]
                ) -> Dict[str, Dict[str, str]]:
    """``{ref: {pin: net}}`` for the whole netlist.

    Needed to tell a resistor from a FET: both bridge the segment and the
    rail, and only one of them is a pull-up. The distinguishing fact is the
    pin count, which lives across all nets, not on either of these two.
    """
    out: Dict[str, Dict[str, str]] = {}
    for net, nodes in nets.items():
        for ref, pin in nodes:
            out.setdefault(ref, {})[pin] = net
    return out


def resolve_pullups(nets: Mapping[str, Sequence[Tuple[str, str]]],
                    components: Mapping[str, Mapping[str, str]],
                    line: str, net: str, rail_net: str) -> Resolution:
    """Find every pull-up on ``net`` by topology, and refuse to guess.

    A pull-up is not "a part whose designator starts with R". It is a
    TWO-TERMINAL part with one pin on the segment and the other on the
    declared rail. Resolving it by designator prefix would count a series
    resistor on the same net and miss a network named ``RP1``.

    Two pins is the whole test, and it has to be, because "bridges the line
    and the rail" alone catches every slave on the bus: an IC with an I/O on
    SDA and a supply pin on VDD touches both nets by construction. Those are
    RECORDED (``other_bridging``) and not modelled - see the tool's limits.
    A FET level shifter is indistinguishable from them here, which is why the
    rail a segment returns to is a declared input rather than a guess.

    A two-terminal part whose value cannot be read as a resistance - ``DNP``,
    a Schottky, a ferrite - IS a gap, not a skipped item: it sits in the DC
    path, so the parallel equivalent is unknown rather than "the sum of the
    parts I recognised". Dropping it would bias the equivalent upwards, which
    is the direction that passes.
    """
    resolution = Resolution(line=line, net=net, rail_net=rail_net)
    if net not in nets:
        resolution.gaps.append(Gap(
            net, SEGMENT_NET_NOT_IN_NETLIST,
            f"net {net!r} ({line}) is not in the netlist; the segment could "
            "not be resolved, so nothing about it was checked"))
    if rail_net not in nets:
        resolution.gaps.append(Gap(
            rail_net, RAIL_NET_NOT_IN_NETLIST,
            f"rail net {rail_net!r} is not in the netlist; the pull-up "
            "network cannot be resolved against a rail that is not there"))
    if resolution.gaps:
        return resolution

    resolution.refs_on_net = sorted({ref for ref, _ in nets[net]})
    rail_refs = {ref for ref, _ in nets[rail_net]}
    pin_map = pins_by_ref(nets)

    for ref in sorted(set(resolution.refs_on_net) & rail_refs):
        pins = pin_map.get(ref, {})
        if len(pins) != 2:
            resolution.other_bridging.append((ref, len(pins)))
            continue
        entry = components.get(ref)
        if entry is None:
            resolution.gaps.append(Gap(
                ref, COMPONENT_NOT_IN_NETLIST,
                f"{ref} is on {net} and {rail_net} but has no component "
                "record in the netlist, so it has no value to read"))
            continue
        try:
            ohms = parse_resistance(entry.get("value"))
        except ValueError as exc:
            resolution.gaps.append(Gap(
                ref, UNPARSEABLE_RESISTOR_VALUE,
                f"{ref} bridges {net} and {rail_net} but its value "
                f"{entry.get('value')!r} is unreadable: {exc}"))
            continue
        line_pin = next(pin for pin, on in pins.items() if on == net)
        rail_pin = next(pin for pin, on in pins.items() if on == rail_net)
        resolution.pullups.append(PullUp(ref=ref,
                                         value_text=str(entry.get("value")),
                                         ohms=ohms, line_pin=line_pin,
                                         rail_pin=rail_pin))

    if resolution.gaps:
        # One unreadable member makes the whole equivalent unknown.
        resolution.pullups = []
        resolution.parallel_ohms = None
        return resolution
    resolution.parallel_ohms = parallel_ohms([p.ohms for p in resolution.pullups])
    return resolution
