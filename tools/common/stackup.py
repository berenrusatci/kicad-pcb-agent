"""Layer-count and stackup planning, plus the placement/DFM screens around it.

Ported from ``pcb-tool-v2/pcb_stackup_planner.py``. That file mixed a library
(dataclasses + pure calculators) with a 170-line ``__main__`` demo that printed
47 lines to stdout. This module is the LIBRARY half only; the CLI half lives in
``scripts/stackup_plan.py`` and emits JSON instead of printing.

WHAT CHANGED IN THE PORT
------------------------
* Public names and JSON keys are English (bundle convention). Turkish stays in
  the human-readable violation/detail prose, which is where the domain
  vocabulary is actually load-bearing. Rename table:

      Net.isim/tur                 -> Net.name/kind
      Komponent                    -> Component
      MekanikVeTermalKisitlar      -> MechanicalThermalLimits
      DiferansiyelCift             -> DiffPair
      ViaKullanimi                 -> ViaUsage
      AntenYerlesimi               -> AntennaPlacement
      KristalYerlesimi             -> CrystalPlacement
      RFHattiStitching             -> RfStitching
      YalitimGereksinimi           -> IsolationRequirement
      YuksekAkimIzi                -> HighCurrentTrace
      TermalYonetim                -> ThermalManagement
      SinyalTuru/KilifTuru/ViaTipi -> SignalKind/PackageKind/ViaKind
      AraBirimTuru/FrekansBandi    -> InterfaceKind/FrequencyBand
      sinyalleri_grupla            -> group_signals
      katman_sayisini_hesapla      -> required_layer_count
      dizilimi_olustur             -> build_stackup
      capraz_dogrulama_yap         -> electrical_drc
      fiziksel_dogrulama_yap       -> physical_drc
      bakir_dengesi_kontrolu       -> copper_balance_delta_pct
      iz_genisligi_hesapla_mm      -> trace_width_mm
      gerekli_bakir_agirligi_oz    -> required_copper_oz
      gerekli_izolasyon_mesafesi_mm-> required_isolation_mm
      rf_stitching_araligi_hesapla -> rf_stitching_pitch_mm
      termal_via_sayisi_hesapla    -> thermal_via_count
      stackup_planla               -> plan_stackup
      FABRIKA_PROFILLERI           -> fab_profiles()  (data, see below)

* Layer roles are no longer matched by substring-searching Turkish prose.
  ``build_stackup`` values are ``"<ROLE> — <Turkish note>"`` where ROLE is one
  of ``SIGNAL`` / ``GND`` / ``POWER`` / ``SIGNAL+POWER``, and every rule uses
  ``layer_roles()`` instead of ``"GND" in text``. The old code asked
  ``"SİNYAL" in value``, which silently depends on the locale of a display
  string; a reworded note would have turned a KRİTİK HATA into a silent pass.

* Every per-item check keeps its ``list[str]`` form as ``*_violations`` and
  gains a plural ``*_check`` that returns a ``common.findings.Finding``, so
  "nothing was examined" reports NO_COVERAGE instead of an empty-list PASS.

* ``FABRIKA_PROFILLERI`` moved out of code into
  ``fixtures/references/fab_profiles.json`` and is read by ``fab_profiles()``.
  A missing/corrupt file raises ``StackupDataError``; there is deliberately NO
  hardcoded fallback, because a silently-substituted fab limit is exactly the
  kind of invisible wrong answer this bundle exists to prevent.

* ``trace_width_mm`` DELEGATES to ``common.si_models.ipc2221_min_width_mm``
  rather than carrying a third copy of the IPC-2221 expression. One numeric
  divergence is inherited and is deliberate: the archive used 1 oz = 1.378 mil
  directly, si_models derives it as ``OZ_TO_MM * MM_TO_MIL`` = 1.3701 mil
  (~0.6% thinner copper, so ~0.6% wider traces). si_models is the canonical
  constant; ``tests/test_stackup.py`` pins the divergence so it cannot drift
  further unnoticed.

HONESTY NOTES CARRIED OVER FROM THE SOURCE (do not delete these):
these are screening models, not sign-off. IPC-2221 is not the IPC-2152 curve
set; the interface impedance/length targets are reminders, not specifications;
the antenna keep-out, crystal placement, stitching, creepage and thermal-via
numbers are typical/conservative rules of thumb. Each function repeats its own
caveat in its docstring.

TUR 3 ADDITION — per-via-type aspect ratio (ported from the archive's
``via_siniflandirma.py``, which did not exist at Tur 1/2 time):

      ViaTipiDetay        -> ViaTypeDetail
      Via                 -> PhysicalVia (renamed: ``Via`` would collide with
                             the existing ``ViaUsage``/``ViaKind`` vocabulary)
      select_via_type_for_bga -> select_via_type_for_bga (unchanged)

The archive kept this in a separate file solely to avoid a circular import
(``via_siniflandirma`` -> ``pcb_stackup_planner``, with the reverse import
deferred into a function body). That constraint does not apply here — both
sides already live in this one module — so ``PhysicalVia`` is defined
directly below ``via_stub_check`` instead of as a second file.

TUR 4 ADDITION — HDI layer-span feasibility, replacing the archive's empty
``hdi_stackup_genisletme.py`` data model with real checks:

      via_layer_span_valid            — does a via's declared (start_layer,
                                        end_layer) match its ``kind`` by
                                        IPC-2226 definition (through-hole
                                        must reach both outer layers, buried
                                        must touch neither, blind/microvia
                                        exactly one)
      stacked_microvia_sequence_valid — a chain of microvias with no gap/
                                        overlap, each covering one layer step
      resolve_stackup_layer_index,
      microvia_span_valid_for_stackup,
      blind_buried_region_valid_for_stackup
                                       — the archive's MikroviaKatmani/
                                        BlindBuriedViaBolgesi fields (layer
                                        NAMES) checked against a REAL
                                        ``build_stackup``/``plan_stackup``
                                        dict, instead of floating
                                        disconnected. The archive's own
                                        "TBD, not invented" fields (an
                                        aspect-ratio number, a fab-capability
                                        flag) are still not invented here —
                                        real aspect-ratio numbers live in
                                        ``MAX_ASPECT_RATIO_BY_VIA_TYPE``.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .findings import Finding, result
from .si_models import (MODEL_CLASS, SIGNOFF, ipc2221_min_width_mm,
                        sweep_stackups)

__all__ = [
    "MODEL_CLASS", "SIGNOFF", "StackupDataError", "StackupPlanError",
    "SignalKind", "PackageKind", "ViaKind", "InterfaceKind", "FrequencyBand",
    "Net", "Component", "MechanicalThermalLimits", "DiffPair", "ViaUsage",
    "AntennaPlacement", "CrystalPlacement", "RfStitching",
    "IsolationRequirement", "HighCurrentTrace", "ThermalManagement",
    "FabProfile", "fab_profiles", "FAB_PROFILES_PATH",
    "group_signals", "has_package", "required_layer_count", "build_stackup",
    "layer_roles", "layer_note", "stackup_as_list",
    "electrical_drc", "physical_drc", "copper_balance_delta_pct",
    "thermal_layer_requirement", "manufacturing_limit_layers",
    "trace_width_mm", "power_trace_widths_mm",
    "fab_dfm_violations", "fab_dfm_check",
    "decoupling_violations", "decoupling_check",
    "impedance_target_ohm", "solve_impedance_geometry",
    "length_matching_violations", "length_matching_check",
    "via_stub_violations", "via_stub_check",
    "ViaTypeDetail", "PhysicalVia", "MAX_ASPECT_RATIO_BY_VIA_TYPE",
    "via_type_aspect_ratio_violations", "via_type_aspect_ratio_check",
    "select_via_type_for_bga",
    "via_layer_span_valid", "stacked_microvia_sequence_valid",
    "resolve_stackup_layer_index", "microvia_span_valid_for_stackup",
    "blind_buried_region_valid_for_stackup",
    "antenna_keepout_violations", "antenna_keepout_check",
    "crystal_placement_violations", "crystal_placement_check",
    "rf_stitching_pitch_mm", "rf_stitching_violations", "rf_stitching_check",
    "required_isolation_mm", "isolation_violations", "isolation_check",
    "required_copper_oz", "power_copper_violations", "power_copper_check",
    "thermal_via_count", "thermal_management_violations",
    "thermal_management_check",
    "plan_stackup",
]


class StackupDataError(RuntimeError):
    """A required reference data table is missing or malformed."""


class StackupPlanError(RuntimeError):
    """No layer count satisfied the constraints within the attempt budget.

    Subclasses RuntimeError so the archive's ``pytest.raises(RuntimeError)``
    contract still holds, while letting the CLI catch it precisely and turn it
    into a FAIL report (exit 1) rather than a crash (exit 2).
    """


# --------------------------------------------------------------------------- #
# 1. Data structures
# --------------------------------------------------------------------------- #
class SignalKind(Enum):
    POWER = auto()
    GND = auto()
    FAST_DIGITAL = auto()
    SLOW_DIGITAL = auto()
    ANALOG = auto()
    RF = auto()


class PackageKind(Enum):
    STANDARD = auto()
    DIP = auto()
    QFN = auto()
    BGA = auto()
    FINE_PITCH_BGA = auto()


class ViaKind(Enum):
    THROUGH_ONLY = auto()
    BLIND_BURIED_ALLOWED = auto()


class InterfaceKind(Enum):
    """High-speed interface type — selects the differential impedance target."""
    USB2_0 = auto()
    USB3_X = auto()
    HDMI = auto()
    ETHERNET_1G = auto()
    LVDS = auto()
    PCIE = auto()
    DDR3_4 = auto()   # usually single-ended, but length matching is critical


class FrequencyBand(Enum):
    """Band used by the RF antenna and transmission-line rules."""
    WIFI_BLE_2G4 = auto()
    WIFI_5G = auto()
    SUBGHZ_433 = auto()
    SUBGHZ_868_915 = auto()
    GPS_L1 = auto()


@dataclass
class Net:
    name: str
    kind: SignalKind
    controlled_impedance: bool = False
    max_current_a: float = 0.0


@dataclass
class Component:
    name: str
    package: PackageKind
    pin_count: int = 0
    power_pin_count: int = 0        # separate VDD/VCC pins on the IC
    decoupling_cap_count: int = 0   # decoupling caps declared for this part


@dataclass
class MechanicalThermalLimits:
    target_thickness_mm: float = 1.6
    max_dissipation_w: float = 1.0
    allowed_via_kind: ViaKind = ViaKind.BLIND_BURIED_ALLOWED
    copper_balance_tolerance_pct: float = 15.0


@dataclass
class DiffPair:
    """Design data for one differential pair (e.g. USB_DP/USB_DM)."""
    name: str
    interface: InterfaceKind
    length_p_mm: float
    length_n_mm: float
    data_rate_gbps: float = 0.0
    target_impedance_ohm_override: float = 0.0  # 0 => take it from the table


@dataclass
class ViaUsage:
    """The layer span a signal actually uses — input to the stub estimate."""
    net_name: str
    start_layer: int
    end_layer: int
    data_rate_gbps: float


@dataclass
class AntennaPlacement:
    """Placement data for a chip antenna or a PCB trace antenna."""
    name: str
    band: FrequencyBand
    edge_clearance_mm: float      # antenna to board edge
    nearest_copper_mm: float      # antenna to the nearest copper pour / GND


@dataclass
class CrystalPlacement:
    """Placement data for a crystal / oscillator."""
    name: str
    ic_pin_distance_mm: float          # distance to the IC's XTAL pins
    noise_source_distance_mm: float    # distance to the nearest fast/RF/switcher
    guard_ring: bool = False           # GND guard ring around the crystal


@dataclass
class RfStitching:
    """GND stitching-via pitch along one RF trace."""
    name: str
    frequency_ghz: float
    via_pitch_mm: float


@dataclass
class IsolationRequirement:
    """High-voltage creepage/clearance requirement between two nets."""
    name: str
    voltage_delta_v: float
    design_clearance_mm: float   # the real physical distance in the layout


@dataclass
class HighCurrentTrace:
    """Current / width / copper-weight data for a power-electronics trace."""
    name: str
    current_a: float
    width_mm: float
    external: bool = True


@dataclass
class ThermalManagement:
    """A component's dissipation and the thermal vias actually drawn."""
    name: str
    dissipation_w: float
    thermal_via_count: int


# --------------------------------------------------------------------------- #
# 2. Signal grouping
# --------------------------------------------------------------------------- #
GROUP_KEYS = ("power", "ground", "critical", "rf", "analog", "standard_io")


def group_signals(nets: Sequence[Net]) -> Dict[str, List[Net]]:
    """Bucket nets into the six groups the layer rules reason about.

    RF nets appear in BOTH ``rf`` and ``critical``: the return-path rules apply
    to RF as well, but RF is tracked separately because it drives its own
    minimum layer count.
    """
    groups: Dict[str, List[Net]] = {key: [] for key in GROUP_KEYS}

    for net in nets:
        if net.kind == SignalKind.POWER:
            groups["power"].append(net)
        elif net.kind == SignalKind.GND:
            groups["ground"].append(net)
        elif net.kind == SignalKind.RF:
            groups["rf"].append(net)
            groups["critical"].append(net)
        elif net.kind == SignalKind.FAST_DIGITAL:
            groups["critical"].append(net)
        elif net.kind == SignalKind.ANALOG:
            groups["analog"].append(net)
        else:                        # SLOW_DIGITAL and anything else
            groups["standard_io"].append(net)

    return groups


# --------------------------------------------------------------------------- #
# 3. Layer-count algorithm
# --------------------------------------------------------------------------- #
BGA_PIN_BUDGET = 600
MANY_CRITICAL_SIGNALS_THRESHOLD = 15


def has_package(components: Sequence[Component], package: PackageKind) -> bool:
    return any(c.package == package for c in components)


def required_layer_count(groups: Dict[str, List[Net]],
                         components: Sequence[Component]) -> int:
    """Minimum layer count from routability, return path and power integrity.

    These are the industry rules of thumb the source encoded; they screen a
    design, they do not replace a routing feasibility study.
    """
    layers = 2   # cheapest starting point

    # Rule 1: physical impossibility (fine-pitch BGA escape routing)
    fine_pitch = sum(1 for c in components
                     if c.package == PackageKind.FINE_PITCH_BGA)
    if fine_pitch >= 1:
        layers = max(layers, 8)

    # Rule 1b: several fine-pitch BGAs, or a very high pin count — escape fans
    # overlap and single-direction routing no longer fits.
    bga_pins = sum(c.pin_count for c in components
                   if c.package == PackageKind.FINE_PITCH_BGA)
    if fine_pitch >= 2 or bga_pins > BGA_PIN_BUDGET:
        layers = max(layers, 10)

    # Rule 2: controlled impedance and a clean return path
    if groups["critical"]:
        layers = max(layers, 4)

    # Rule 2b: RF wants GND on BOTH sides of the RF layer, so the RF trace can
    # be buried between two separate references => at least 6.
    if groups["rf"]:
        layers = max(layers, 6)

    # Rule 3: power integrity (several rails + many critical signals)
    if len(groups["power"]) >= 3 and len(groups["critical"]) > 10:
        layers = max(layers, 6)

    # Rule 3b: a >3 A rail wants its own thick copper plane
    high_current = any(n.max_current_a > 3.0 for n in groups["power"])
    if high_current and len(groups["power"]) >= 2:
        layers = max(layers, 6)

    return layers


# --------------------------------------------------------------------------- #
# 4. Stackup assignment
# --------------------------------------------------------------------------- #
ROLE_SIGNAL = "SIGNAL"
ROLE_GND = "GND"
ROLE_POWER = "POWER"
ROLE_MIXED = "SIGNAL+POWER"

# Density weights for the warp/copper-balance screen. Order matters: a mixed
# layer scores as SIGNAL, exactly as the source's first-match-wins loop did.
_ROLE_DENSITY = ((ROLE_SIGNAL, 1.0), (ROLE_POWER, 0.9), (ROLE_GND, 0.6))
_UNKNOWN_DENSITY = 0.5


def _layer(role: str, note: str) -> str:
    return f"{role} — {note}"


def layer_roles(layer_value: str) -> set:
    """The role token(s) of a stackup entry.

    Replaces the source's substring search over Turkish display prose. A mixed
    signal+power layer legitimately carries two roles.
    """
    token = str(layer_value).split("—", 1)[0].strip()
    if not token:
        return set()
    return set(part for part in token.split("+") if part)


def layer_note(layer_value: str) -> str:
    """The human-readable Turkish note of a stackup entry."""
    parts = str(layer_value).split("—", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def stackup_as_list(stackup: Dict[str, str]) -> List[dict]:
    """``{"layer_1": "GND — ..."}`` -> a report-friendly ordered list."""
    rows = []
    for index in range(1, len(stackup) + 1):
        value = stackup.get(f"layer_{index}", "")
        rows.append({
            "index": index,
            "roles": sorted(layer_roles(value)),
            "note": layer_note(value),
        })
    return rows


def build_stackup(layer_count: int,
                  groups: Dict[str, List[Net]]) -> Dict[str, str]:
    """Assign a role to every layer. Keys are ``layer_1`` .. ``layer_N``."""
    stackup: Dict[str, str] = {}

    if layer_count == 2:
        stackup["layer_1"] = _layer(
            ROLE_MIXED, "SİNYAL + GÜÇ karışık, referans zayıf")
        stackup["layer_2"] = _layer(
            ROLE_GND, "tüm dönüş yolu tek katmanda")

    elif layer_count == 4:
        stackup["layer_1"] = _layer(
            ROLE_SIGNAL, "öncelik: kritik sinyaller ve analog")
        stackup["layer_2"] = _layer(ROLE_GND, "katman 1'in referansı")
        stackup["layer_3"] = _layer(
            ROLE_POWER, "katman 4'ün referansı, katman 2 ile kapasitans")
        stackup["layer_4"] = _layer(ROLE_SIGNAL, "öncelik: standart I/O")

    elif layer_count == 6:
        if len(groups["critical"]) > MANY_CRITICAL_SIGNALS_THRESHOLD:
            # Shielding variant: the most critical signals get buried.
            stackup["layer_1"] = _layer(ROLE_SIGNAL, "standart")
            stackup["layer_2"] = _layer(ROLE_GND, "referans")
            stackup["layer_3"] = _layer(
                ROLE_SIGNAL, "en kritik sinyaller buraya gömülür")
            stackup["layer_4"] = _layer(
                ROLE_POWER, "katman 3'ü alttan kalkanlar (GND de olabilir)")
            stackup["layer_5"] = _layer(ROLE_POWER, "geri kalan voltajlar")
            stackup["layer_6"] = _layer(ROLE_SIGNAL, "standart")
        else:
            stackup["layer_1"] = _layer(ROLE_SIGNAL, "kritik/analog")
            stackup["layer_2"] = _layer(ROLE_GND, "referans")
            stackup["layer_3"] = _layer(ROLE_SIGNAL, "standart I/O")
            stackup["layer_4"] = _layer(ROLE_POWER, "güç düzlemi")
            stackup["layer_5"] = _layer(ROLE_GND, "referans")
            stackup["layer_6"] = _layer(ROLE_SIGNAL, "standart I/O")

    else:
        # 8 layers and up: outer layers signal, critical signals buried
        # between GND planes, power planes towards the middle.
        stackup["layer_1"] = _layer(ROLE_SIGNAL, "kritik/analog - dış")
        stackup["layer_2"] = _layer(ROLE_GND, "referans")
        remaining = layer_count - 4
        inner_signal_layers = max(1, remaining // 2)
        index = 3
        for i in range(inner_signal_layers):
            stackup[f"layer_{index}"] = _layer(
                ROLE_SIGNAL, "gömülü, GND ile korumalı")
            index += 1
            stackup[f"layer_{index}"] = _layer(
                ROLE_POWER if i % 2 == 0 else ROLE_GND, "iç düzlem")
            index += 1
        while index <= layer_count - 2:
            stackup[f"layer_{index}"] = _layer(ROLE_POWER, "güç düzlemi")
            index += 1
        stackup[f"layer_{layer_count - 1}"] = _layer(ROLE_GND, "referans")
        stackup[f"layer_{layer_count}"] = _layer(
            ROLE_SIGNAL, "standart I/O - dış")

    return stackup


# --------------------------------------------------------------------------- #
# 5. Electrical DRC
# --------------------------------------------------------------------------- #
def electrical_drc(stackup: Dict[str, str], groups: Dict[str, List[Net]],
                   layer_count: int) -> Tuple[List[str], List[str]]:
    """Return-path, plane-splitting, crosstalk and RF-isolation screens.

    Returns ``(errors, warnings)``. Errors force another layer-count attempt;
    warnings are notes for the person routing the board.
    """
    errors: List[str] = []
    warnings: List[str] = []

    # CHECK 1: return path
    if groups["critical"]:
        if ROLE_GND not in layer_roles(stackup.get("layer_2", "")):
            errors.append(
                "KRİTİK HATA: Katman 1'deki hızlı sinyaller için L2'de temiz "
                "bir GND dönüş yolu yok!")

    # CHECK 2: power-plane fragmentation
    power_layers = sum(1 for value in stackup.values()
                       if ROLE_POWER in layer_roles(value))
    if len(groups["power"]) >= 4 and power_layers < 2:
        errors.append(
            "KRİTİK HATA: Tek bir GÜÇ katmanında 4 farklı voltaj bölünemez. "
            "EMI yayılımı felaket olur!")

    # CHECK 3: adjacent signal layers -> crosstalk
    for index in range(1, layer_count):
        current = layer_roles(stackup.get(f"layer_{index}", ""))
        following = layer_roles(stackup.get(f"layer_{index + 1}", ""))
        if ROLE_SIGNAL in current and ROLE_SIGNAL in following:
            warnings.append(
                f"UYARI: Katman {index} ve {index + 1} yan yana sinyal. "
                f"Çizerken bu yolları birbirine DİK (X ve Y ekseninde) çiz!")

    # CHECK 4: RF isolation — RF needs to sit between two GND references.
    if groups["rf"] and layer_count < 6:
        errors.append(
            "KRİTİK HATA: RF sinyalleri var ama katman sayısı RF'i iki GND "
            "arasına gömmeye yetmiyor (min. 6 katman gerekir).")

    return errors, warnings


# --------------------------------------------------------------------------- #
# 6. Physical / manufacturing DRC
# --------------------------------------------------------------------------- #
LAYER_THICKNESS_ASSUMPTION_MM = 0.15
DEFAULT_MIN_VIA_DIAMETER_MM = 0.2
DEFAULT_MAX_ASPECT_RATIO = 8.0


def thermal_layer_requirement(limits: MechanicalThermalLimits,
                              current_layers: int) -> int:
    """Bump the layer count when the board must spread real heat.

    Crude screen: above 5 W a 2/4-layer board rarely has enough copper area.
    Real thermal design needs an FEA or the vendor's application note.
    """
    if limits.max_dissipation_w > 5.0 and current_layers < 6:
        return current_layers + 2
    return current_layers


def manufacturing_limit_layers(components: Sequence[Component],
                               limits: MechanicalThermalLimits,
                               current_layers: int) -> int:
    """Fine-pitch BGA escape with through-only vias needs more layers."""
    if has_package(components, PackageKind.FINE_PITCH_BGA):
        if limits.allowed_via_kind == ViaKind.THROUGH_ONLY:
            return max(current_layers, 10)
    return current_layers


def copper_balance_delta_pct(stackup: Dict[str, str], layer_count: int) -> float:
    """Simplified copper-density asymmetry between the top and bottom halves.

    In real manufacturing this is computed from copper-pour AREA percentages.
    Here it is a density score derived from the layer roles only, so treat it
    as a warp-risk smell test rather than a measurement.
    """
    def score(layer_value: str) -> float:
        roles = layer_roles(layer_value)
        for role, weight in _ROLE_DENSITY:
            if role in roles:
                return weight
        return _UNKNOWN_DENSITY

    middle = layer_count / 2
    top_total = 0.0
    bottom_total = 0.0
    for index in range(1, layer_count + 1):
        value = score(stackup.get(f"layer_{index}", ""))
        if index <= middle:
            top_total += value
        else:
            bottom_total += value

    top_density = (top_total / (layer_count / 2)) * 100
    bottom_density = (bottom_total / (layer_count / 2)) * 100
    return abs(top_density - bottom_density)


def physical_drc(stackup: Dict[str, str], limits: MechanicalThermalLimits,
                 components: Sequence[Component],
                 layer_count: int) -> List[str]:
    """Thickness, warp and through-via aspect-ratio screens."""
    errors: List[str] = []

    # CHECK 1: mechanical thickness conflict
    min_thickness = layer_count * LAYER_THICKNESS_ASSUMPTION_MM
    if min_thickness > limits.target_thickness_mm:
        errors.append(
            f"MEKANİK HATA: {layer_count} katmanlı yapı, "
            f"{limits.target_thickness_mm}mm kalınlığa sığmaz "
            f"(min. gerekli ~{min_thickness:.2f}mm)!")

    # CHECK 2: warping risk
    delta = copper_balance_delta_pct(stackup, layer_count)
    if delta > limits.copper_balance_tolerance_pct:
        errors.append(
            f"ÜRETİM HATASI: Bakır dağılımı asimetrik (fark: %{delta:.1f}, "
            f"tolerans: %{limits.copper_balance_tolerance_pct}). Kart lehim "
            f"fırınında bükülecektir. Boş katmanlara (thieving) bakır dökümü "
            f"ekle.")

    # CHECK 3: through-via aspect ratio. Industry rule of thumb: thickness /
    # diameter must not exceed 8:1 or the fab cannot plate the barrel
    # reliably.
    if limits.allowed_via_kind == ViaKind.THROUGH_ONLY:
        ratio = limits.target_thickness_mm / DEFAULT_MIN_VIA_DIAMETER_MM
        if ratio > DEFAULT_MAX_ASPECT_RATIO:
            errors.append(
                f"ÜRETİM HATASI: Boydan-boya via delme oranı {ratio:.1f}:1, "
                f"izinli {DEFAULT_MAX_ASPECT_RATIO:.0f}:1 sınırını aşıyor. "
                f"Kör/gömülü via'ya izin verilmeli ya da kart kalınlığı "
                f"azaltılmalı.")

    return errors


# --------------------------------------------------------------------------- #
# 7. Trace width / current capacity (IPC-2221 screen)
# --------------------------------------------------------------------------- #
def trace_width_mm(current_a: float, delta_t_c: float = 10.0,
                   copper_oz: float = 1.0, *, external: bool = True) -> float:
    """Minimum trace width in mm for ``current_a``, IPC-2221.

        A = (I / (k * dT^0.44)) ^ (1/0.725)

    A is the cross-section in mil^2, I the current in A, dT the allowed
    temperature rise in °C, and k is 0.048 for an external layer / 0.024 for an
    internal one (inner layers shed heat less easily, so they need a wider
    trace for the same current).

    THIS IS IPC-2221, NOT THE FULL IPC-2152 CURVE SET. IPC-2152 additionally
    models airflow, neighbouring-trace temperature and multi-layer coupling and
    is not reproduced here. On critical or high-current designs use this as a
    FIRST ESTIMATE only and confirm with a field solver or the fabricator's own
    calculator before manufacturing.

    ``current_a <= 0`` returns 0.0 (a net with no declared current is "not
    applicable", not an error). The formula itself lives in
    ``common.si_models.ipc2221_min_width_mm`` — this is the domain-facing
    wrapper, not a second copy.
    """
    if current_a <= 0:
        return 0.0
    return ipc2221_min_width_mm(
        current_a, delta_t_c, copper_oz,
        "microstrip" if external else "stripline")


def power_trace_widths_mm(groups: Dict[str, List[Net]],
                          copper_oz: float = 1.0,
                          delta_t_c: float = 10.0) -> Dict[str, float]:
    """Recommended minimum EXTERNAL-layer width (mm) per current-carrying rail.

    Nets with ``max_current_a == 0`` are skipped: no declared current means no
    recommendation, not a 0 mm recommendation.
    """
    recommendations: Dict[str, float] = {}
    for net in groups["power"]:
        if net.max_current_a > 0:
            recommendations[net.name] = trace_width_mm(
                net.max_current_a, delta_t_c, copper_oz, external=True)
    return recommendations


# --------------------------------------------------------------------------- #
# 8. Fabricator DFM profiles (data lives in fixtures/references)
# --------------------------------------------------------------------------- #
FAB_PROFILES_PATH = (Path(__file__).resolve().parent.parent
                     / "fixtures" / "references" / "fab_profiles.json")

FAB_PROFILES_SCHEMA = "fab-profiles/v1"


@dataclass(frozen=True)
class FabProfile:
    """One fabricator capability set.

    THESE NUMBERS ARE REPRESENTATIVE. Before committing to manufacturing,
    re-verify them against the CURRENT capabilities page of the fabricator you
    picked: they change over time and differ by service/option.
    """
    key: str
    name: str
    min_track_mm: float
    min_clearance_mm: float
    min_hole_mm: float
    min_annular_ring_mm: float
    max_aspect_ratio: float = 8.0


_FAB_PROFILE_CACHE: Dict[str, FabProfile] | None = None


def _load_fab_profiles(path: Path) -> Dict[str, FabProfile]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StackupDataError(
            f"fab profile table missing: {path}. There is deliberately no "
            f"built-in fallback — a silently substituted fab limit would turn "
            f"a DFM gate into a guess.") from exc
    except (OSError, ValueError) as exc:
        raise StackupDataError(f"cannot read {path}: {exc}") from exc

    if raw.get("schema_version") != FAB_PROFILES_SCHEMA:
        raise StackupDataError(
            f"{path}: expected schema_version {FAB_PROFILES_SCHEMA!r}, "
            f"got {raw.get('schema_version')!r}")

    profiles: Dict[str, FabProfile] = {}
    for key, entry in (raw.get("profiles") or {}).items():
        limits = entry.get("limits") or {}
        try:
            profiles[key] = FabProfile(
                key=key,
                name=entry["name"],
                min_track_mm=float(limits["min_trace_mm"]),
                min_clearance_mm=float(limits["min_space_mm"]),
                min_hole_mm=float(limits["min_finished_hole_mm"]),
                min_annular_ring_mm=float(limits["min_annular_ring_mm"]),
                max_aspect_ratio=float(limits.get("max_aspect_ratio", 8.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StackupDataError(
                f"{path}: profile {key!r} is incomplete: {exc}") from exc
    if not profiles:
        raise StackupDataError(f"{path}: no profiles defined")
    return profiles


def fab_profiles(path: Path | None = None) -> Dict[str, FabProfile]:
    """The fabricator capability table, keyed ``JLCPCB_STANDARD`` etc.

    Reads ``fixtures/references/fab_profiles.json`` (UTF-8). Raises
    ``StackupDataError`` when the file is missing or malformed; it never falls
    back to hardcoded limits.
    """
    global _FAB_PROFILE_CACHE
    if path is not None:
        return _load_fab_profiles(Path(path))
    if _FAB_PROFILE_CACHE is None:
        _FAB_PROFILE_CACHE = _load_fab_profiles(FAB_PROFILES_PATH)
    return dict(_FAB_PROFILE_CACHE)


def fab_dfm_violations(track_mm: float, clearance_mm: float, hole_mm: float,
                       profile: FabProfile) -> List[str]:
    """Compare three design values against one fab profile."""
    violations: List[str] = []

    if track_mm < profile.min_track_mm:
        violations.append(
            f"DFM HATASI ({profile.name}): İz genişliği {track_mm}mm, "
            f"minimum {profile.min_track_mm}mm'nin altında.")

    if clearance_mm < profile.min_clearance_mm:
        violations.append(
            f"DFM HATASI ({profile.name}): İz aralığı {clearance_mm}mm, "
            f"minimum {profile.min_clearance_mm}mm'nin altında.")

    if hole_mm < profile.min_hole_mm:
        violations.append(
            f"DFM HATASI ({profile.name}): Delik çapı {hole_mm}mm, "
            f"minimum {profile.min_hole_mm}mm'nin altında.")

    return violations


def fab_dfm_check(track_mm: float, clearance_mm: float, hole_mm: float,
                  profile: FabProfile) -> Finding:
    """``fab_dfm_violations`` as a Finding; scanned = the three rules checked."""
    violations = fab_dfm_violations(track_mm, clearance_mm, hole_mm, profile)
    return result("fab_dfm_limits", 3, violations,
                  f"profil={profile.name}; iz/aralık/delik değerleri "
                  f"karşılaştırıldı. Değerler temsilidir — üreticinin güncel "
                  f"yetenek sayfasıyla doğrula.")


# --------------------------------------------------------------------------- #
# 9. Decoupling capacitors
# --------------------------------------------------------------------------- #
BULK_CAP_PIN_THRESHOLD = 100


def decoupling_violations(component: Component) -> List[str]:
    """One decoupling cap (typically 100 nF) per power pin.

    COUNT ONLY. Whether the capacitor is actually placed within a few mm of the
    pin is a separate placement/routing check and is NOT modelled here.
    """
    violations: List[str] = []
    if (component.power_pin_count > 0
            and component.decoupling_cap_count < component.power_pin_count):
        violations.append(
            f"DEKUPLAJ HATASI: {component.name} için "
            f"{component.power_pin_count} güç pini var ama sadece "
            f"{component.decoupling_cap_count} decoupling kapasitör tanımlı. "
            f"Her güç pinine en az 1 adet (tipik 100nF) yakın kapasitör ekle.")
    if (component.pin_count >= BULK_CAP_PIN_THRESHOLD
            and component.decoupling_cap_count < component.power_pin_count + 1):
        violations.append(
            f"BULK KAPASİTÖR UYARISI: {component.name} yüksek pinli bir "
            f"komponent; ani akım talepleri için decoupling kapasitörlerine ek "
            f"olarak en az 1 adet bulk kapasitör (10-100uF) eklenmesi önerilir.")
    return violations


def decoupling_check(components: Sequence[Component]) -> Finding:
    violations: List[str] = []
    for component in components:
        violations.extend(decoupling_violations(component))
    return result("decoupling_caps", len(components), violations,
                  "yalnız SAYI kontrolü; kapasitörün pine fiziksel yakınlığı "
                  "ayrı bir yerleşim kontrolüdür ve modellenmemiştir")


# --------------------------------------------------------------------------- #
# 10. Differential pair impedance target
# --------------------------------------------------------------------------- #
# These are common industry targets taken from the interface specifications.
# In a real design the achievable impedance depends on board thickness,
# dielectric constant and trace geometry and MUST be confirmed with a field
# solver / stackup calculator. This is a reminder table, not a specification.
INTERFACE_IMPEDANCE_TARGETS_OHM: Dict[InterfaceKind, float] = {
    InterfaceKind.USB2_0: 90.0,
    InterfaceKind.USB3_X: 90.0,
    InterfaceKind.HDMI: 100.0,
    InterfaceKind.ETHERNET_1G: 100.0,
    InterfaceKind.LVDS: 100.0,
    InterfaceKind.PCIE: 85.0,
    InterfaceKind.DDR3_4: 40.0,   # single-ended characteristic impedance
}


def impedance_target_ohm(pair: DiffPair) -> float:
    if pair.target_impedance_ohm_override > 0:
        return pair.target_impedance_ohm_override
    return INTERFACE_IMPEDANCE_TARGETS_OHM.get(pair.interface, 100.0)


def solve_impedance_geometry(pair: DiffPair, h_mm: float, er: float,
                             t_mm: float = 0.035,
                             fab_min_w_mm: float = 0.127,
                             fab_min_s_mm: float = 0.127) -> Dict[str, object]:
    """Solve the (W, S) that actually MEETS ``impedance_target_ohm(pair)``.

    ``impedance_target_ohm`` only returns a number to aim at; nothing computed
    the geometry that hits it. This closes that gap and should be called
    immediately after it.

    Returns ``{"target_ohm", "best": {w_mm, s_mm, zdiff_ohm, error_percent} |
    None, "candidates": [...], "reachable": bool}``. ``best is None`` means the
    target is UNREACHABLE with the given H and fab limits — revise the stackup,
    not the routing.

    Screening only: ``model_class = exploratory_closed_form``, ``signoff =
    False``. Production sign-off needs real laminate data plus a fab impedance
    coupon / TDR report.

    The (W,S) sweep is ``common.si_models.sweep_stackups``. It used to live in
    ``scripts/zdiff_solver.py``, so this module imported UPWARDS out of
    ``common/`` into ``scripts/`` and only worked when tools/ happened to be on
    sys.path. The solver is a pure function of the closed-form models, so it
    was moved down into ``common/si_models.py``; zdiff_solver.py is now a CLI
    over the same function, which also guarantees the CLI and this call site
    can never diverge numerically.
    """
    target_ohm = impedance_target_ohm(pair)
    report = sweep_stackups(
        target_ohm=target_ohm, er=er, t=t_mm,
        w_range=(0.05, 2.0, 0.005), s_range=(0.05, 2.0, 0.005),
        fab_min_w=fab_min_w_mm, fab_min_s=fab_min_s_mm,
        h_candidates=(h_mm,),
    )
    row = report["stackups"][0]
    return {
        "target_ohm": target_ohm,
        "best": row["best"] if row["reachable"] else None,
        "candidates": row["top_5"],
        "reachable": row["reachable"],
        "model_class": MODEL_CLASS,
        "signoff": SIGNOFF,
    }


# --------------------------------------------------------------------------- #
# 11. Length matching
# --------------------------------------------------------------------------- #
# Tolerances vary per interface; these are typical/conservative values. For a
# critical design take the exact tolerance from the interface's own electrical
# specification (USB-IF, HDMI Forum, JEDEC, ...).
INTERFACE_LENGTH_TOLERANCE_MM: Dict[InterfaceKind, float] = {
    InterfaceKind.USB2_0: 1.5,
    InterfaceKind.USB3_X: 0.5,
    InterfaceKind.HDMI: 0.5,
    InterfaceKind.ETHERNET_1G: 1.0,
    InterfaceKind.LVDS: 0.5,
    InterfaceKind.PCIE: 0.13,
    InterfaceKind.DDR3_4: 0.13,
}


def length_matching_violations(pair: DiffPair) -> List[str]:
    """P/N length skew of one differential pair against its tolerance."""
    violations: List[str] = []
    delta_mm = abs(pair.length_p_mm - pair.length_n_mm)
    tolerance = INTERFACE_LENGTH_TOLERANCE_MM.get(pair.interface, 0.5)

    if delta_mm > tolerance:
        violations.append(
            f"LENGTH MATCHING HATASI ({pair.name}): P/N hatları arasında "
            f"{delta_mm:.3f}mm fark var, {pair.interface.name} için izin "
            f"verilen tolerans {tolerance}mm. Kısa olan hatta 'serpentine' "
            f"(kıvrımlı) routing ile uzunluk ekle.")
    return violations


def length_matching_check(pairs: Sequence[DiffPair]) -> Finding:
    violations: List[str] = []
    for pair in pairs:
        violations.extend(length_matching_violations(pair))
    return result("length_matching", len(pairs), violations,
                  "toleranslar tipik/muhafazakâr; kesin değer arayüzün resmî "
                  "elektriksel şartnamesinden alınmalı")


# --------------------------------------------------------------------------- #
# 12. Via stub (back-drilling need)
# --------------------------------------------------------------------------- #
# Rule of thumb only. The real stub effect depends on frequency, dielectric and
# via geometry; a definite answer needs an SI simulation. The common industry
# screen "above ~10 Gbps with a stub over ~0.5 mm, consider back-drilling" is
# what is implemented here. ``scripts/board/via_stub.py`` MEASURES the same
# thing from a real board and supersedes this estimate when a board exists.
STUB_LENGTH_THRESHOLD_MM = 0.5
DATA_RATE_THRESHOLD_GBPS = 10.0


def via_stub_violations(via: ViaUsage, layer_count: int,
                        via_kind: ViaKind) -> List[str]:
    violations: List[str] = []

    if via_kind != ViaKind.THROUGH_ONLY:
        # A blind/buried via leaves no stub: it spans only the used layers.
        return violations

    stub_layers = layer_count - via.end_layer
    stub_mm = stub_layers * LAYER_THICKNESS_ASSUMPTION_MM

    if (via.data_rate_gbps >= DATA_RATE_THRESHOLD_GBPS
            and stub_mm > STUB_LENGTH_THRESHOLD_MM):
        violations.append(
            f"VIA STUB UYARISI ({via.net_name}): {via.data_rate_gbps} Gbps "
            f"hızında sinyal, boydan-boya via ile katman {via.end_layer}'de "
            f"bitiyor ama karta katman {layer_count}'e kadar delik açılıyor. "
            f"Tahmini stub uzunluğu ~{stub_mm:.2f}mm — bu hızda sinyal "
            f"yansımasına (reflection) yol açabilir. Back-drilling veya "
            f"kör/gömülü via kullanımı önerilir.")
    return violations


def via_stub_check(vias: Sequence[ViaUsage], layer_count: int,
                   via_kind: ViaKind) -> Finding:
    violations: List[str] = []
    for via in vias:
        violations.extend(via_stub_violations(via, layer_count, via_kind))
    return result("via_stub_estimate", len(vias), violations,
                  f"kural-of-thumb: >={DATA_RATE_THRESHOLD_GBPS} Gbps ve stub "
                  f">{STUB_LENGTH_THRESHOLD_MM}mm; katman kalınlığı "
                  f"{LAYER_THICKNESS_ASSUMPTION_MM}mm VARSAYILDI. Gerçek ölçüm "
                  f"için board üzerinde scripts/board/via_stub.py kullan.")


# --------------------------------------------------------------------------- #
# 12b. Per-via-type aspect ratio (Tur 3 addition, ported from
# pcb-tool-v2/via_siniflandirma.py)
# --------------------------------------------------------------------------- #
# ``via_stub_check`` above and ``physical_drc``'s aspect-ratio rule both reason
# at BOARD/POLICY level: "does this design allow blind/buried vias at all",
# "what is the single aspect-ratio ceiling for the whole board" (see
# ``DEFAULT_MAX_ASPECT_RATIO`` / ``FabProfile.max_aspect_ratio``). A real board
# can mix via types even while that policy is enabled (e.g. a through-hole
# power-plane via next to a microvia under a fine-pitch BGA), and each type
# has its OWN manufacturable ceiling — blind/buried/microvia need a laser or
# controlled-depth drill and are reliable only to about 1:1, while a
# mechanically-drilled through-hole via is reliable to
# ``DEFAULT_MAX_ASPECT_RATIO`` (8:1). This section checks EACH via against the
# ceiling for ITS OWN type, which the board-level policy check cannot do.
class ViaTypeDetail(Enum):
    """A via's actual physical construction (IPC-2226 classification)."""
    THROUGH_HOLE = auto()   # drills through every layer
    BLIND = auto()           # outer layer to an inner layer
    BURIED = auto()          # between two inner layers, invisible from outside
    MICROVIA = auto()        # usually L1-L2 (or Ln-1 - Ln), laser-drilled


# Maximum reliable aspect ratio (drill depth / drill diameter) BY VIA TYPE.
# THROUGH_HOLE is intentionally the SAME constant as
# ``DEFAULT_MAX_ASPECT_RATIO`` used by ``physical_drc`` above — both trace back
# to the same industry-standard 8:1 through-hole plating limit (IPC-2221/2226),
# not two independently maintained numbers. BLIND/BURIED/MICROVIA are 1:1 —
# laser or controlled-depth mechanical drilling generally cannot exceed that
# (source: IPC-2226 HDI design guide, Type I-III microvia structures).
MAX_ASPECT_RATIO_BY_VIA_TYPE: Dict[ViaTypeDetail, float] = {
    ViaTypeDetail.THROUGH_HOLE: DEFAULT_MAX_ASPECT_RATIO,
    ViaTypeDetail.BLIND: 1.0,
    ViaTypeDetail.BURIED: 1.0,
    ViaTypeDetail.MICROVIA: 1.0,
}


@dataclass
class PhysicalVia:
    """One via's full physical description, for per-type aspect-ratio DRC.

    Field names deliberately mirror ``ViaUsage`` (net_name/start_layer/
    end_layer) where the concepts overlap. Not merged into ``ViaUsage``
    itself: that dataclass is signal-integrity/stub focused
    (``data_rate_gbps`` has no physical-DRC counterpart here), this one is
    physical-construction focused (``drill_diameter_mm``/``in_pad`` have no
    stub-estimate counterpart there).
    """
    net_name: str
    start_layer: int
    end_layer: int
    kind: ViaTypeDetail
    drill_diameter_mm: float
    pad_diameter_mm: float
    in_pad: bool = False              # via-in-pad
    filled_and_capped: bool = False   # IPC-4761 Type VII (fill + cap)

    @property
    def layer_span(self) -> int:
        """Number of layers the via passes through (inclusive difference)."""
        return abs(self.end_layer - self.start_layer)

    @property
    def aspect_ratio(self) -> float:
        """Drill ratio = drilled depth / drill diameter.

        Depth uses the same ``LAYER_THICKNESS_ASSUMPTION_MM`` (0.15 mm/layer)
        as ``via_stub_violations`` above — not redefined here.
        """
        if self.drill_diameter_mm <= 0:
            return float("inf")
        depth_mm = self.layer_span * LAYER_THICKNESS_ASSUMPTION_MM
        return depth_mm / self.drill_diameter_mm

    @property
    def max_allowed_aspect_ratio(self) -> float:
        return MAX_ASPECT_RATIO_BY_VIA_TYPE[self.kind]


def via_type_aspect_ratio_violations(via: PhysicalVia) -> List[str]:
    limit = via.max_allowed_aspect_ratio
    ratio = via.aspect_ratio
    if ratio <= limit:
        return []
    return [
        f"VIA TİP/ASPECT ORANI ({via.net_name}): {via.kind.name} via için "
        f"ölçülen aspect oranı {ratio:.3f}:1, izinli sınır {limit:.0f}:1 "
        f"(katman {via.start_layer}->{via.end_layer}, matkap "
        f"{via.drill_diameter_mm}mm). KÖR/GÖMÜLÜ/MİKROVİA lazer/kör delik "
        f"teknolojisi ~1:1 gerektirir; BOYDAN-BOYA mekanik delik "
        f"{DEFAULT_MAX_ASPECT_RATIO:.0f}:1'e kadar güvenilir metalize olur."]


def via_type_aspect_ratio_check(vias: Sequence[PhysicalVia]) -> Finding:
    """Per-via aspect-ratio screen against ITS OWN type's ceiling.

    Distinct from ``physical_drc``'s board-level aspect-ratio rule, which
    applies one policy-wide ceiling to every via regardless of type — see the
    module comment above this section.
    """
    violations: List[str] = []
    for via in vias:
        violations.extend(via_type_aspect_ratio_violations(via))
    return result(
        "via_type_aspect_ratio", len(vias), violations,
        "her via KENDİ tipine (BOYDAN-BOYA/KÖR/GÖMÜLÜ/MİKROVİA) göre "
        "değerlendirildi; katman kalınlığı "
        f"{LAYER_THICKNESS_ASSUMPTION_MM}mm VARSAYILDI.")


def select_via_type_for_bga(
        pad_pitch_mm: float,
        routing_layer_gap: int,
        microvia_drill_mm: float = 0.1,
        blind_drill_mm: float = 0.15,
        through_hole_drill_mm: float = 0.3,
        min_annular_ring_mm: float = 0.1,
        start_layer: int = 1) -> PhysicalVia:
    """Pick a via type from BGA/fine-pitch pad density (proactive, before
    placement/routing starts) rather than measuring an already-drawn board.

    Decision table:
      - pitch >= 0.8mm                                  -> THROUGH_HOLE
      - 0.5mm <= pitch < 0.8mm AND routing_layer_gap is
        shallow (1-2 layers)                              -> BLIND
      - pitch < 0.5mm, OR no room to reach a neighbouring
        pad                                                -> MICROVIA (+in_pad)

    HONESTY NOTE (an acknowledged simplification, carried over from the
    source): in the 0.5-0.8mm pitch band, ``routing_layer_gap > 2`` (not
    shallow) falls into the third branch (MICROVIA) — but a real microvia
    alone cannot practically span more than 1-2 layers (laser drill depth
    limit). The real manufacturing answer there is a STACKED/STAGGERED
    microvia sequence, not a single microvia. This function still returns the
    nearest/finest category (MICROVIA) for classification purposes and sets
    ``in_pad=True`` to force the IPC-4761 Type VII requirement; a caller that
    sees ``routing_layer_gap > 2`` AND ``kind == MICROVIA`` together must
    separately recognise that this needs a via-stack design (this function
    does not model the stack itself).

    When via-in-pad is selected, ``filled_and_capped`` is forced True —
    IPC-4761 Type VII (fill + cap) is mandatory there, so the fab note carries
    that requirement automatically.
    """
    if pad_pitch_mm <= 0:
        raise ValueError(f"pad_pitch_mm must be positive, got {pad_pitch_mm!r}")
    if routing_layer_gap < 1:
        raise ValueError(
            f"routing_layer_gap must be >= 1, got {routing_layer_gap!r}")

    shallow = routing_layer_gap <= 2

    if pad_pitch_mm >= 0.8:
        kind = ViaTypeDetail.THROUGH_HOLE
        drill = through_hole_drill_mm
        in_pad = False
    elif pad_pitch_mm >= 0.5 and shallow:
        kind = ViaTypeDetail.BLIND
        drill = blind_drill_mm
        in_pad = False
    else:
        kind = ViaTypeDetail.MICROVIA
        drill = microvia_drill_mm
        in_pad = True

    pad = drill + 2 * min_annular_ring_mm
    return PhysicalVia(
        net_name="",
        start_layer=start_layer,
        end_layer=start_layer + routing_layer_gap,
        kind=kind,
        drill_diameter_mm=round(drill, 4),
        pad_diameter_mm=round(pad, 4),
        in_pad=in_pad,
        filled_and_capped=in_pad,
    )


# --------------------------------------------------------------------------- #
# 12c. HDI layer-span feasibility (Tur 4 addition)
# --------------------------------------------------------------------------- #
# via_type_aspect_ratio_check above asks "is THIS via's drill ratio
# manufacturable for its declared type". This section asks a DIFFERENT
# question: does the via's declared (start_layer, end_layer) pair even
# MATCH what that type means by IPC-2226 definition? A "BLIND" via spanning
# two outer layers is a through-hole via mislabelled; a "BURIED" via
# touching an outer layer is not buried at all - both are declaration
# errors a caller could make when hand-building a PhysicalVia, and neither
# is caught by the aspect-ratio check (a via can have a perfectly
# manufacturable drill ratio while lying about what kind of via it is).
def via_layer_span_valid(via: PhysicalVia, layer_count: int) -> Tuple[bool, str]:
    """Does ``via``'s (start_layer, end_layer) pair match its declared
    ``kind`` by IPC-2226 definition, on a board with ``layer_count`` total
    layers (1-indexed, layer 1 = top/L1, layer ``layer_count`` = bottom)?

    Returns ``(valid, reason)``; ``reason`` is populated only when invalid.

      - THROUGH_HOLE: by definition drills the WHOLE board - both ends must
        be the two outer layers (1 and layer_count). A "through-hole" that
        does not reach both outer surfaces is not physically a through-hole
        via - it should be declared BLIND or BURIED instead.
      - BLIND: exactly ONE end must be an outer layer, the other an inner
        layer strictly between the two outer layers. A via spanning two
        outer layers is a through-hole by definition, not blind; a via
        between two inner layers is buried, not blind.
      - BURIED: BOTH ends must be inner layers - by definition, invisible
        from either outer surface. A via touching an outer layer at either
        end is not buried.
      - MICROVIA: same outer-to-adjacent-inner shape as BLIND, but
        additionally restricted to exactly ONE layer step - a real
        microvia is a single laser-drilled layer-pair connection. A
        multi-layer "microvia" is not one via; see
        ``stacked_microvia_sequence_valid`` for that case.
    """
    if layer_count < 2:
        raise ValueError(f"layer_count must be >= 2, got {layer_count!r}")
    low, high = sorted((via.start_layer, via.end_layer))
    if not (1 <= low and high <= layer_count):
        return False, (
            f"{via.net_name}: layers {via.start_layer}/{via.end_layer} fall "
            f"outside the board's 1..{layer_count} range")

    is_outer = lambda layer: layer == 1 or layer == layer_count  # noqa: E731
    low_outer, high_outer = is_outer(low), is_outer(high)

    if via.kind == ViaTypeDetail.THROUGH_HOLE:
        if not (low == 1 and high == layer_count):
            return False, (
                f"{via.net_name}: declared THROUGH_HOLE but only spans "
                f"layers {low}..{high}, not both outer layers 1..{layer_count} "
                "- a mechanical through-drill always reaches both surfaces")
        return True, ""

    if via.kind == ViaTypeDetail.BURIED:
        if low_outer or high_outer:
            return False, (
                f"{via.net_name}: declared BURIED but touches an outer "
                f"layer ({low if low_outer else high}) - a buried via must "
                "be invisible from both outer surfaces")
        return True, ""

    if via.kind == ViaTypeDetail.BLIND:
        if low_outer == high_outer:
            reason = ("spans two outer layers - that is a through-hole"
                      if low_outer else
                      "spans two inner layers - that is a buried via")
            return False, (
                f"{via.net_name}: declared BLIND but {reason}, not blind "
                "(exactly one end must be an outer layer)")
        return True, ""

    if via.kind == ViaTypeDetail.MICROVIA:
        # A microvia is defined by a SINGLE laser-drilled layer step, not by
        # touching an outer surface - a sequential build-up HDI stack chains
        # microvias between INNER layer pairs too (see
        # stacked_microvia_sequence_valid), so no outer/inner constraint is
        # applied here beyond the single-step span.
        if via.layer_span != 1:
            return False, (
                f"{via.net_name}: declared MICROVIA spanning {via.layer_span} "
                "layers - a real microvia is a single laser-drilled layer "
                "step; see stacked_microvia_sequence_valid() for a "
                "multi-layer microvia connection")
        return True, ""

    raise ValueError(f"unhandled ViaTypeDetail: {via.kind!r}")


def stacked_microvia_sequence_valid(
        vias: Sequence[PhysicalVia], layer_count: int,
        ) -> Tuple[bool, str]:
    """Does a SEQUENCE of microvias form a valid stacked/staggered chain
    connecting its overall start layer to its overall end layer?

    ``select_via_type_for_bga``'s own documented limit: a single microvia
    cannot practically span more than 1-2 layers (laser drill depth), so a
    connection deeper than that needs a STACK of individually-valid
    microvias, each covering exactly one layer step, chained end to end
    with no gap and no overlap. This function checks the CHAIN's integrity;
    each individual via's own type/span validity is still
    ``via_type_aspect_ratio_check``/``via_layer_span_valid``'s job.

    Returns ``(valid, reason)``. Requires at least 2 vias in the sequence
    (a single microvia is not a "stack" - it is just a microvia, see
    ``via_layer_span_valid``); an empty or single-via sequence raises
    ``ValueError`` rather than silently answering a question it cannot
    meaningfully answer.
    """
    if len(vias) < 2:
        raise ValueError(
            "stacked_microvia_sequence_valid needs at least 2 vias - a "
            "single microvia is not a stack (use via_layer_span_valid)")

    non_microvia = [v for v in vias if v.kind != ViaTypeDetail.MICROVIA]
    if non_microvia:
        return False, (
            "sequence contains a non-microvia via "
            f"({non_microvia[0].net_name}: {non_microvia[0].kind.name}) - "
            "a stacked sequence must be microvias only")

    ordered = sorted(vias, key=lambda v: min(v.start_layer, v.end_layer))
    expected_start = min(min(v.start_layer, v.end_layer) for v in ordered)
    expected_end = max(max(v.start_layer, v.end_layer) for v in ordered)

    cursor = expected_start
    for via in ordered:
        low, high = sorted((via.start_layer, via.end_layer))
        if low != cursor:
            return False, (
                f"gap or overlap in the stack: expected the next microvia "
                f"to start at layer {cursor}, {via.net_name} starts at "
                f"layer {low}")
        valid, reason = via_layer_span_valid(via, layer_count)
        if not valid:
            return False, f"stack member invalid on its own: {reason}"
        cursor = high

    if cursor != expected_end:
        return False, (
            f"the stack does not reach its own overall end layer "
            f"({cursor} != {expected_end})")
    return True, ""


# --------------------------------------------------------------------------- #
# 12d. Binding HDI via checks to a REAL stackup (Tur 4 addition)
# --------------------------------------------------------------------------- #
# The archive's hdi_stackup_genisletme.py carried three EMPTY dataclasses
# (MikroviaKatmani/BlindBuriedViaBolgesi/HdiStackupGenisletmesi) whose own
# docstrings named the exact connection point they were waiting for:
# plan_stackup()'s "layer_N" naming (archived as "Katman_N"). Rather than
# port those containers as-is and leave them just as empty, this section
# IS that connection - it resolves a stackup's own "layer_N" names to the
# integer indices via_layer_span_valid already validates against, so a
# caller can describe an HDI via the way the archive's fields did (by
# layer NAME, against a real stackup) instead of by bare integers.
#
# NOT carried over from the archive, on purpose: MikroviaKatmani's
# ``hedef_delik_capi_mm``/``aspect_orani_siniri`` and
# BlindBuriedViaBolgesi's ``fabrika_destekliyor_mu`` were the archive's own
# "TBD, not invented" fields - no IPC-2226 aspect-ratio number or fab
# capability was guessed there, and none is guessed here either;
# via_type_aspect_ratio_check already owns the real (non-invented)
# aspect-ratio numbers, from MAX_ASPECT_RATIO_BY_VIA_TYPE.

def resolve_stackup_layer_index(layer_name: str, stackup: Dict[str, str]) -> int:
    """``"layer_N"`` -> ``N``, validated against a REAL stackup dict (as
    produced by ``build_stackup``/``plan_stackup``) - raises
    ``StackupDataError`` for a name that does not exist in THIS stackup,
    rather than parsing a number out of a string that names nothing real.
    """
    if layer_name not in stackup:
        raise StackupDataError(
            f"{layer_name!r} is not a layer in this stackup "
            f"(has: {sorted(stackup)})")
    try:
        return int(layer_name.rsplit("_", 1)[-1])
    except ValueError as exc:
        raise StackupDataError(
            f"{layer_name!r} does not match the 'layer_N' naming "
            "convention") from exc


def microvia_span_valid_for_stackup(
        top_layer_name: str, bottom_layer_name: str,
        stackup: Dict[str, str]) -> Tuple[bool, str]:
    """Validate a microvia declared by LAYER NAME (the archive's
    ``MikroviaKatmani.ust_katman_adi``/``alt_katman_adi`` shape) against a
    real stackup dict, via ``via_layer_span_valid``. ``(valid, reason)``.
    """
    top = resolve_stackup_layer_index(top_layer_name, stackup)
    bottom = resolve_stackup_layer_index(bottom_layer_name, stackup)
    via = PhysicalVia("", top, bottom, ViaTypeDetail.MICROVIA,
                      drill_diameter_mm=0.1, pad_diameter_mm=0.2)
    return via_layer_span_valid(via, len(stackup))


def blind_buried_region_valid_for_stackup(
        start_layer_name: str, end_layer_name: str, buried: bool,
        stackup: Dict[str, str]) -> Tuple[bool, str]:
    """Validate a blind/buried via region declared by LAYER NAME (the
    archive's ``BlindBuriedViaBolgesi.baslangic_katman_adi``/
    ``bitis_katman_adi``/``buried_mi`` shape) against a real stackup dict,
    via ``via_layer_span_valid``. ``(valid, reason)``.
    """
    start = resolve_stackup_layer_index(start_layer_name, stackup)
    end = resolve_stackup_layer_index(end_layer_name, stackup)
    kind = ViaTypeDetail.BURIED if buried else ViaTypeDetail.BLIND
    via = PhysicalVia("", start, end, kind,
                      drill_diameter_mm=0.1, pad_diameter_mm=0.2)
    return via_layer_span_valid(via, len(stackup))


# --------------------------------------------------------------------------- #
# 13. Antenna keep-out
# --------------------------------------------------------------------------- #
# TYPICAL/conservative figures from chip-antenna application notes. The keep-out
# drawing in the antenna module's OWN datasheet is the real reference; the
# spread between antennas (especially sub-GHz and GPS patch/external antennas)
# is large. This screen is only "better than not checking at all".
ANTENNA_MIN_COPPER_CLEARANCE_MM: Dict[FrequencyBand, float] = {
    FrequencyBand.WIFI_BLE_2G4: 5.0,
    FrequencyBand.WIFI_5G: 5.0,
    FrequencyBand.SUBGHZ_433: 15.0,
    FrequencyBand.SUBGHZ_868_915: 12.0,
    FrequencyBand.GPS_L1: 5.0,
}

ANTENNA_MIN_EDGE_CLEARANCE_MM: Dict[FrequencyBand, float] = {
    FrequencyBand.WIFI_BLE_2G4: 3.0,
    FrequencyBand.WIFI_5G: 3.0,
    FrequencyBand.SUBGHZ_433: 5.0,
    FrequencyBand.SUBGHZ_868_915: 5.0,
    FrequencyBand.GPS_L1: 3.0,
}


def antenna_keepout_violations(antenna: AntennaPlacement) -> List[str]:
    violations: List[str] = []

    min_copper = ANTENNA_MIN_COPPER_CLEARANCE_MM.get(antenna.band, 5.0)
    min_edge = ANTENNA_MIN_EDGE_CLEARANCE_MM.get(antenna.band, 3.0)

    if antenna.nearest_copper_mm < min_copper:
        violations.append(
            f"ANTEN KEEP-OUT HATASI ({antenna.name}): En yakın bakır/GND "
            f"dökümüne {antenna.nearest_copper_mm}mm mesafede, "
            f"{antenna.band.name} için minimum {min_copper}mm keep-out "
            f"gerekir. Anten performansı ve rezonans frekansı bakırdan "
            f"etkilenir; alandaki bakır dökümünü çıkar.")

    if antenna.edge_clearance_mm < min_edge:
        violations.append(
            f"ANTEN KEEP-OUT HATASI ({antenna.name}): Kart kenarına "
            f"{antenna.edge_clearance_mm}mm mesafede, minimum {min_edge}mm "
            f"gerekir. Anten kenara çok yakınsa ışıma paterni bozulur ve "
            f"verim düşer.")

    return violations


def antenna_keepout_check(antennas: Sequence[AntennaPlacement]) -> Finding:
    violations: List[str] = []
    for antenna in antennas:
        violations.extend(antenna_keepout_violations(antenna))
    return result("antenna_keepout", len(antennas), violations,
                  "tipik uygulama-notu rakamları; asıl referans anten "
                  "modülünün kendi datasheet keep-out çizimidir")


# --------------------------------------------------------------------------- #
# 14. Crystal / oscillator placement
# --------------------------------------------------------------------------- #
# General best-practice guidance. The exact distance depends on the crystal's
# frequency and the IC vendor's layout guide — on a critical design follow the
# "layout guidelines" section of the IC datasheet instead.
CRYSTAL_MAX_IC_DISTANCE_MM = 8.0
CRYSTAL_MIN_NOISE_DISTANCE_MM = 5.0


def crystal_placement_violations(crystal: CrystalPlacement) -> List[str]:
    violations: List[str] = []

    if crystal.ic_pin_distance_mm > CRYSTAL_MAX_IC_DISTANCE_MM:
        violations.append(
            f"KRİSTAL YERLEŞİM HATASI ({crystal.name}): IC'nin XTAL pinlerine "
            f"{crystal.ic_pin_distance_mm}mm mesafede, maksimum önerilen "
            f"{CRYSTAL_MAX_IC_DISTANCE_MM}mm. Uzun XTAL izleri parazitik "
            f"kapasitans ve EMI'ya yol açar; kristali IC'ye mümkün olduğunca "
            f"yaklaştır.")

    if (crystal.noise_source_distance_mm < CRYSTAL_MIN_NOISE_DISTANCE_MM
            and not crystal.guard_ring):
        violations.append(
            f"KRİSTAL YERLEŞİM HATASI ({crystal.name}): En yakın "
            f"gürültülü/hızlı sinyale {crystal.noise_source_distance_mm}mm "
            f"mesafede ve etrafında GND guard ring yok. Kristal hassas bir "
            f"analog osilatördür; ya {CRYSTAL_MIN_NOISE_DISTANCE_MM}mm'den "
            f"uzağa taşı ya da etrafına guard ring ekle.")

    return violations


def crystal_placement_check(crystals: Sequence[CrystalPlacement]) -> Finding:
    violations: List[str] = []
    for crystal in crystals:
        violations.extend(crystal_placement_violations(crystal))
    return result("crystal_placement", len(crystals), violations,
                  "genel iyi-uygulama mesafeleri; kesin değer IC datasheet'inin "
                  "layout guidelines bölümünden alınmalı")


# --------------------------------------------------------------------------- #
# 15. RF GND stitching-via pitch
# --------------------------------------------------------------------------- #
def rf_stitching_pitch_mm(frequency_ghz: float, er_eff: float = 3.0) -> float:
    """lambda/20 stitching-via pitch, mm.

    Uses c = 300 mm/ns and a SIMPLIFIED effective dielectric constant. The
    lambda/20 (some sources say lambda/10) rule of thumb is where the GND
    return path is still considered continuous. The exact wavelength depends on
    trace width and stackup and must be confirmed with a field solver.

    ``frequency_ghz <= 0`` returns +inf: with no frequency there is no limit to
    violate, which keeps the check from inventing a failure.
    """
    if frequency_ghz <= 0:
        return float("inf")
    wavelength_mm = 300.0 / (frequency_ghz * math.sqrt(er_eff))
    return wavelength_mm / 20.0


def rf_stitching_violations(trace: RfStitching,
                            er_eff: float = 3.0) -> List[str]:
    violations: List[str] = []
    allowed = rf_stitching_pitch_mm(trace.frequency_ghz, er_eff)

    if trace.via_pitch_mm > allowed:
        violations.append(
            f"RF STITCHING HATASI ({trace.name}): GND stitching via aralığı "
            f"{trace.via_pitch_mm}mm, {trace.frequency_ghz}GHz için izin "
            f"verilen maksimum ~{allowed:.2f}mm (λ/20). Aralık çok genişse "
            f"dönüş yolu sürekliliği bozulur ve istenmeyen ışıma/kayıp oluşur. "
            f"Via sayısını artır.")
    return violations


def rf_stitching_check(traces: Sequence[RfStitching],
                       er_eff: float = 3.0) -> Finding:
    violations: List[str] = []
    for trace in traces:
        violations.extend(rf_stitching_violations(trace, er_eff))
    return result("rf_stitching_pitch", len(traces), violations,
                  f"λ/20 kuralı, er_eff={er_eff} VARSAYILDI; gerçek değer iz "
                  f"genişliği ve stackup'a bağlıdır")


# --------------------------------------------------------------------------- #
# 16. Creepage / clearance
# --------------------------------------------------------------------------- #
# TYPICAL/simplified IPC-2221B values for an EXTERNAL, UNCOATED conductor at
# sea level with a low pollution degree. The real requirement changes
# SIGNIFICANTLY with conformal coating, altitude, pollution degree and the
# applicable safety standard (IEC 60950, 62368, 60601 for medical, ...). For
# anything with real high voltage, use the CURRENT table of the applicable
# standard and have a safety/compliance engineer confirm it — this is a first
# screen only.
IPC2221_EXTERNAL_CLEARANCE_TABLE_MM: List[Tuple[float, float]] = [
    (15, 0.05),
    (30, 0.05),
    (50, 0.1),
    (100, 0.1),
    (150, 0.2),
    (170, 0.2),
    (250, 0.3),
    (500, 0.5),
    (1000, 1.5),
]


def required_isolation_mm(voltage_delta_v: float) -> float:
    """Minimum external-conductor clearance for a voltage difference, mm.

    Above the table's highest row the value is extrapolated LINEARLY, which is
    a crude approximation and must be checked against the real table.
    """
    for max_voltage, clearance_mm in IPC2221_EXTERNAL_CLEARANCE_TABLE_MM:
        if voltage_delta_v <= max_voltage:
            return clearance_mm
    last_voltage, last_clearance = IPC2221_EXTERNAL_CLEARANCE_TABLE_MM[-1]
    return last_clearance * (voltage_delta_v / last_voltage)


def isolation_violations(requirement: IsolationRequirement) -> List[str]:
    violations: List[str] = []
    required = required_isolation_mm(requirement.voltage_delta_v)

    if requirement.design_clearance_mm < required:
        violations.append(
            f"YALITIM HATASI ({requirement.name}): "
            f"{requirement.voltage_delta_v}V fark için tasarımdaki mesafe "
            f"{requirement.design_clearance_mm}mm, IPC-2221 tablosuna göre "
            f"minimum {required}mm gerekir. Bu, güvenlik açısından KRİTİK bir "
            f"hatadır — ark oluşumu/kısa devre riski taşır. Hatları uzaklaştır "
            f"veya slot/kesim ekle.")
    return violations


def isolation_check(requirements: Sequence[IsolationRequirement]) -> Finding:
    violations: List[str] = []
    for requirement in requirements:
        violations.extend(isolation_violations(requirement))
    return result("creepage_clearance", len(requirements), violations,
                  "IPC-2221B harici/kaplamasız, deniz seviyesi, düşük "
                  "kirlenme derecesi VARSAYILDI; ilgili güvenlik standardının "
                  "güncel tablosuyla doğrulanmalı")


# --------------------------------------------------------------------------- #
# 17. High-current copper weight
# --------------------------------------------------------------------------- #
# The inverse of trace_width_mm: when the width is fixed (a connector pin/pad,
# say), how much copper weight does the current need?

# Practical/economic ceiling for most standard fab processes. Heavier copper
# (4-6 oz) exists but costs significantly more and not every fab offers it —
# adjust to the capabilities page of the fab you picked.
MAX_ECONOMIC_COPPER_OZ = 3.0


def required_copper_oz(current_a: float, width_mm: float,
                       delta_t_c: float = 10.0, *,
                       external: bool = True) -> float:
    """Copper weight (oz) needed to carry ``current_a`` in a fixed width.

    Same IPC-2221 caveat as ``trace_width_mm``: screening estimate, not
    IPC-2152. Non-positive current or width returns 0.0.
    """
    if current_a <= 0 or width_mm <= 0:
        return 0.0
    # Solve trace_width_mm for copper_oz: width scales as 1/copper_oz, so the
    # required weight is (width at 1 oz) / (available width). This keeps ONE
    # implementation of the IPC-2221 expression (in si_models) rather than
    # inlining the exponents a second time.
    width_at_1oz_mm = trace_width_mm(current_a, delta_t_c, 1.0,
                                     external=external)
    return width_at_1oz_mm / width_mm


def power_copper_violations(
        trace: HighCurrentTrace, delta_t_c: float = 10.0,
        max_economic_oz: float = MAX_ECONOMIC_COPPER_OZ) -> List[str]:
    violations: List[str] = []
    needed_oz = required_copper_oz(trace.current_a, trace.width_mm, delta_t_c,
                                   external=trace.external)

    if needed_oz > max_economic_oz:
        violations.append(
            f"GÜÇ ELEKTRONİĞİ HATASI ({trace.name}): {trace.current_a}A akımı "
            f"{trace.width_mm}mm genişlikte taşımak için ~{needed_oz:.1f}oz "
            f"bakır gerekir, bu ekonomik/standart sınır olan "
            f"{max_economic_oz}oz'un üzerinde. İz genişliğini artır, akımı "
            f"paralel izlere/katmanlara böl veya daha kalın bakır seçeneğini "
            f"fabrikayla teyit et.")
    return violations


def power_copper_check(
        traces: Sequence[HighCurrentTrace], delta_t_c: float = 10.0,
        max_economic_oz: float = MAX_ECONOMIC_COPPER_OZ) -> Finding:
    violations: List[str] = []
    for trace in traces:
        violations.extend(
            power_copper_violations(trace, delta_t_c, max_economic_oz))
    return result("power_copper_weight", len(traces), violations,
                  f"IPC-2221 ön-elemesi (IPC-2152 DEĞİL); ekonomik tavan "
                  f"{max_economic_oz}oz olarak VARSAYILDI")


# --------------------------------------------------------------------------- #
# 18. Thermal vias
# --------------------------------------------------------------------------- #
# Real thermal-via performance depends on via diameter, plating thickness,
# fill/tenting and copper area, and is measured with JEDEC thermal test boards.
# The "each via carries about X watts" rule of thumb below is SIMPLIFIED; a
# critical thermal design needs an FEA or the vendor's application note.
THERMAL_CAPACITY_PER_VIA_W = 0.3   # crude figure for a typical 0.3 mm plated via


def thermal_via_count(power_w: float,
                      per_via_w: float = THERMAL_CAPACITY_PER_VIA_W) -> int:
    if power_w <= 0:
        return 0
    return math.ceil(power_w / per_via_w)


def thermal_management_violations(item: ThermalManagement) -> List[str]:
    violations: List[str] = []
    needed = thermal_via_count(item.dissipation_w)

    if item.thermal_via_count < needed:
        violations.append(
            f"TERMAL YÖNETİM HATASI ({item.name}): {item.dissipation_w}W ısı "
            f"yayılımı için tahmini {needed} termal via gerekir, tasarımda "
            f"sadece {item.thermal_via_count} adet var. Yetersiz termal via, "
            f"komponentin aşırı ısınmasına ve ömrünün kısalmasına yol açar. "
            f"Via sayısını artır veya komponent altındaki bakır alanı büyüt.")
    return violations


def thermal_management_check(items: Sequence[ThermalManagement]) -> Finding:
    violations: List[str] = []
    for item in items:
        violations.extend(thermal_management_violations(item))
    return result("thermal_vias", len(items), violations,
                  f"via başına {THERMAL_CAPACITY_PER_VIA_W}W kural-of-thumb "
                  f"VARSAYILDI; kritik termal tasarım FEA ister")


# --------------------------------------------------------------------------- #
# 19. The iterative planning loop
# --------------------------------------------------------------------------- #
def plan_stackup(nets: Sequence[Net], components: Sequence[Component],
                 limits: MechanicalThermalLimits,
                 max_attempts: int = 10) -> Dict[str, object]:
    """Find the smallest layer count that passes every rule.

    Returns a plan dict::

        {"layer_count", "layers", "attempts": [{"layer_count", "errors"}],
         "warnings", "groups"}

    The archive version PRINTED each attempt and returned only the stackup
    dict; the caller could not report what was tried. Nothing here writes to
    stdout — ``scripts/stackup_plan.py`` serialises this.

    Raises ``StackupPlanError`` (a RuntimeError) when the constraints cannot be
    satisfied within ``max_attempts``, because "no valid stackup" is an answer
    the caller must not mistake for success.
    """
    groups = group_signals(nets)

    # 1) electrical minimum
    layer_count = required_layer_count(groups, components)

    # 2) thermal and manufacturing overrides
    layer_count = max(layer_count,
                      thermal_layer_requirement(limits, layer_count))
    layer_count = max(layer_count,
                      manufacturing_limit_layers(components, limits,
                                                 layer_count))

    attempts: List[dict] = []
    for _ in range(max_attempts):
        stackup = build_stackup(layer_count, groups)
        electrical_errors, warnings = electrical_drc(stackup, groups,
                                                     layer_count)
        physical_errors = physical_drc(stackup, limits, components,
                                       layer_count)
        errors = electrical_errors + physical_errors
        attempts.append({"layer_count": layer_count, "errors": errors})

        if not errors:
            return {
                "layer_count": layer_count,
                "layers": stackup,
                "attempts": attempts,
                "warnings": warnings,
                "groups": {key: [net.name for net in value]
                           for key, value in groups.items()},
            }
        # PCB layer counts always step by two, to keep the core symmetric.
        layer_count += 2

    raise StackupPlanError(
        f"{max_attempts} denemede geçerli bir stackup bulunamadı "
        f"(son deneme: {attempts[-1]['layer_count']} katman). "
        f"Kısıtları gözden geçirin.")
