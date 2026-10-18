"""A registry for the vendor models every simulation in this bundle rests on.

WHAT WAS MISSING
--------------------------------------------------------------------------
Every number this toolchain produces from a simulation inherits the authority
of the model it ran on. ``sim/sparam.py`` reads a Touchstone, ``sim/ngspice.py``
reads a subcircuit, ``sim/loop_stability.py`` reads a sweep, ``board/rail_budget.py``
reads a PSRR figure. Not one of them records WHERE that model came from, WHAT
it is valid for, or WHETHER the bytes on disk are still the ones somebody
vetted. A result whose model cannot be named is not reproducible, and a result
whose model was silently swapped is worse: it still reads as evidence.

``docs/PROVENANCE.json`` already does this for CODE that was imported into the
tree. This is the same discipline for the DATA the simulations consume.

WHAT THIS IS NOT
--------------------------------------------------------------------------
It is not a model store, a downloader or a converter. It ships no models and
fetches nothing. It also does not judge whether a model is ACCURATE - that is
``sim/correlate.py``'s job, and it needs a measurement to do it. The two are
complementary and the difference is worth stating plainly:

    correlate  answers "is this model right?"     (needs a measurement)
    this       answers "which model, and may I use it HERE?"

THE ENVELOPE IS THE POINT
--------------------------------------------------------------------------
A model has a range it was characterised over, and outside that range every
reader in this bundle will still return a number. ``skrf`` will interpolate
between the last two points of an s2p and hand back an S21 for a frequency
the file never measured; a PSRR quoted at 100 kHz will be read as though it
held at 10 MHz. Neither raises. Both are confident and wrong.

So ``valid_range`` is REQUIRED, never defaulted, and a query outside it is
``OUT_OF_ENVELOPE`` -> NO_COVERAGE. Not a clamped value, not an extrapolation,
not a warning next to a number somebody will copy: no number at all.

AND THE ENVELOPE IS ITSELF CHECKED
--------------------------------------------------------------------------
A declaration is a claim, and this one is easy to overclaim - the model was
characterised to 10 GHz, somebody writes 20 GHz because that is what the
channel needs. Where the file can be read, the DECLARED range is compared
against what the file actually contains, and a declaration that exceeds its
own file is ``ENVELOPE_OVERCLAIMS`` -> FAIL. This is the one check here that
nothing else in the bundle can make.

The reading is delegated to ``common.tline.read_touchstone``. Writing a second
Touchstone reader here would give the repo two answers to one question, which
is the #51 and #54 failure exactly.

AUTHORITY DECIDES WHAT A MODEL MAY CLOSE
--------------------------------------------------------------------------
    vendor_published   the vendor's own file or document. May open a sign-off
                       gate.
    vendor_derived     fitted or extracted by us from vendor data. A real
                       result, but SCREEN_ONLY for sign-off - the fit is ours
                       and the vendor never validated it.
    synthetic          built to exercise the machinery (an ideal line, a
                       two-pole response). NEVER sign-off, and a run that used
                       one says so in its report rather than in a footnote.

``authority`` has NO DEFAULT. Defaulting to the weakest tier sounds safe and
is not: it silently demotes a genuine vendor file, and the demotion is
invisible at the point where somebody wonders why the gate will not open.
Defaulting to the strongest is obviously wrong. An unlabelled model does not
load.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .hashing import sha256_file

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

KIND_TOUCHSTONE = "touchstone"
KIND_SPICE = "spice"
KIND_IBIS = "ibis"
KIND_CURVE = "curve"          # a digitised datasheet curve (PSRR, R_DS(on) ...)
KIND_DATASET = "dataset"      # anything else with a declared envelope
KINDS = (KIND_TOUCHSTONE, KIND_SPICE, KIND_IBIS, KIND_CURVE, KIND_DATASET)

AUTHORITY_PUBLISHED = "vendor_published"
AUTHORITY_DERIVED = "vendor_derived"
AUTHORITY_SYNTHETIC = "synthetic"
AUTHORITIES = (AUTHORITY_PUBLISHED, AUTHORITY_DERIVED, AUTHORITY_SYNTHETIC)

# Only a vendor-published model may close a sign-off gate. The other two
# produce real numbers and are reported as such; they just do not open doors.
SIGNOFF_AUTHORITIES = (AUTHORITY_PUBLISHED,)

USE_SIGNOFF = "signoff"
USE_SCREENING = "screening"
USES = (USE_SIGNOFF, USE_SCREENING)

# What a query established, kept separate from PASS so a reader cannot mistake
# a screening clearance for a sign-off clearance.
GRANT_SIGNOFF = "signoff"
GRANT_SCREEN_ONLY = "screen_only"
GRANT_NONE = "none"

# Provenance fields. ``accessed`` is in the list because a vendor document is
# a moving target: "TI SLVSCE9" without a date names a family of documents.
PROVENANCE_FIELDS = ("vendor", "document", "revision", "accessed")

# Reason codes.
MODEL_ABSENT = "MODEL_ABSENT"
STALE_MODEL = "STALE_MODEL"
DIGEST_UNVERIFIED = "DIGEST_UNVERIFIED"
OUT_OF_ENVELOPE = "OUT_OF_ENVELOPE"
ENVELOPE_OVERCLAIMS = "ENVELOPE_OVERCLAIMS"
ENVELOPE_UNCHECKED = "ENVELOPE_UNCHECKED"
AXIS_UNDECLARED = "AXIS_UNDECLARED"
AUTHORITY_INSUFFICIENT = "AUTHORITY_INSUFFICIENT"
SUPERSEDED = "SUPERSEDED"
UNKNOWN_MODEL = "UNKNOWN_MODEL"

# Run-level verdicts, same ladder the assertion layer uses.
GREEN = "GREEN"
RED = "RED"
SCREEN_ONLY = "SCREEN_ONLY"


class ModelSyntaxError(ValueError):
    """A registry entry could not be loaded.

    Raised at LOAD time for the same reason ``assertions.py`` does it: an
    entry that only blows up the day somebody queries it is a model that is
    unvetted on every run where nobody does.
    """

    def __init__(self, model_id: str, code: str, detail: str):
        self.model_id = model_id
        self.code = code
        self.detail = detail
        super().__init__(f"{model_id}: {code} - {detail}")

    def to_dict(self) -> dict:
        return {"model": self.model_id, "issue": self.code,
                "detail": self.detail}


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Axis:
    """One dimension a model was characterised over.

    ``unit`` is mandatory and never inferred. The lesson is the same one
    ``assertions.UNITS_UNBOUND`` encodes: nobody downstream can catch a
    mislabelled unit, but everybody can catch an unlabelled number being
    compared against a labelled one.
    """

    quantity: str
    unit: str
    minimum: float
    maximum: float

    def contains(self, value: float, *, tolerance: float = 0.0) -> bool:
        return (self.minimum - tolerance) <= value <= (self.maximum + tolerance)

    def to_dict(self) -> dict:
        return {"quantity": self.quantity, "unit": self.unit,
                "min": self.minimum, "max": self.maximum}


def load_axis(raw: Any, model_id: str) -> Axis:
    if not isinstance(raw, Mapping):
        raise ModelSyntaxError(model_id, "BAD_ENVELOPE",
                               "each valid_range entry must be an object")
    quantity = raw.get("quantity")
    unit = raw.get("unit")
    if not isinstance(quantity, str) or not quantity.strip():
        raise ModelSyntaxError(model_id, "BAD_ENVELOPE",
                               "an envelope axis needs a 'quantity'")
    if not isinstance(unit, str) or not unit.strip():
        raise ModelSyntaxError(
            model_id, "UNIT_UNDECLARED",
            f"axis {quantity!r} declares no unit; a range of 0..10 is not a "
            "range until somebody says whether that is Hz, GHz or degrees")
    low, high = raw.get("min"), raw.get("max")
    for name, value in (("min", low), ("max", high)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelSyntaxError(model_id, "BAD_ENVELOPE",
                                   f"axis {quantity!r} needs a numeric {name!r}")
        if not math.isfinite(float(value)):
            raise ModelSyntaxError(model_id, "BAD_ENVELOPE",
                                   f"axis {quantity!r} has a non-finite {name!r}")
    if float(high) < float(low):
        raise ModelSyntaxError(
            model_id, "BAD_ENVELOPE",
            f"axis {quantity!r} has max < min; an inverted range accepts "
            "nothing and would read as a model that is never usable")
    return Axis(quantity=quantity.strip(), unit=unit.strip(),
                minimum=float(low), maximum=float(high))


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VendorModel:
    id: str
    kind: str
    path: str
    authority: str
    provenance: Dict[str, str]
    envelope: Tuple[Axis, ...]
    digest: Optional[str] = None        # as REGISTERED, not as found on disk
    supersedes: Tuple[str, ...] = ()
    notes: str = ""

    def axis(self, quantity: str) -> Optional[Axis]:
        for entry in self.envelope:
            if entry.quantity == quantity:
                return entry
        return None

    def may_signoff(self) -> bool:
        return self.authority in SIGNOFF_AUTHORITIES

    def to_dict(self) -> dict:
        payload = {
            "id": self.id, "kind": self.kind, "path": self.path,
            "authority": self.authority,
            "may_signoff": self.may_signoff(),
            "provenance": dict(self.provenance),
            "valid_range": [a.to_dict() for a in self.envelope],
        }
        if self.digest:
            payload["registered_sha256"] = self.digest
        if self.supersedes:
            payload["supersedes"] = list(self.supersedes)
        if self.notes:
            payload["notes"] = self.notes
        return payload


def load_model(entry: Any) -> VendorModel:
    """Build one registry entry, refusing anything that would be usable but
    unaccountable."""
    if not isinstance(entry, Mapping):
        raise ModelSyntaxError("<unknown>", "NOT_AN_OBJECT",
                               "each model must be a JSON object")
    model_id = entry.get("id") if isinstance(entry.get("id"), str) else "<unnamed>"
    if not model_id.strip() or model_id == "<unnamed>":
        raise ModelSyntaxError(model_id, "MISSING_FIELD",
                               "'id' must be a non-empty string")

    kind = entry.get("kind")
    if kind not in KINDS:
        raise ModelSyntaxError(model_id, "BAD_KIND",
                               f"kind must be one of {list(KINDS)}, got {kind!r}")

    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ModelSyntaxError(model_id, "MISSING_FIELD",
                               "'path' must be a non-empty string")

    authority = entry.get("authority")
    if authority not in AUTHORITIES:
        raise ModelSyntaxError(
            model_id, "AUTHORITY_UNDECLARED",
            f"authority must be one of {list(AUTHORITIES)}, got {authority!r}. "
            "There is no default: demoting an unlabelled vendor file to the "
            "weakest tier hides the demotion at exactly the point somebody "
            "wonders why a gate will not open")

    provenance_raw = entry.get("provenance")
    if not isinstance(provenance_raw, Mapping):
        raise ModelSyntaxError(model_id, "PROVENANCE_INCOMPLETE",
                               "'provenance' must be an object")
    missing = [f for f in PROVENANCE_FIELDS
               if not str(provenance_raw.get(f, "")).strip()]
    if missing:
        raise ModelSyntaxError(
            model_id, "PROVENANCE_INCOMPLETE",
            f"provenance is missing {missing}. A vendor document without a "
            "revision and an access date names a FAMILY of documents, and the "
            "one that was read is not recoverable later")
    provenance = {f: str(provenance_raw[f]).strip() for f in PROVENANCE_FIELDS}
    for key, value in provenance_raw.items():
        if key not in provenance:
            provenance[str(key)] = str(value)

    envelope_raw = entry.get("valid_range")
    if not isinstance(envelope_raw, Sequence) or isinstance(envelope_raw, str) \
            or not envelope_raw:
        raise ModelSyntaxError(
            model_id, "ENVELOPE_UNDECLARED",
            "'valid_range' must be a non-empty array. A model with no declared "
            "range is a model every reader in this bundle will happily "
            "extrapolate, and extrapolation here does not raise - it returns a "
            "confident number")
    envelope = tuple(load_axis(axis, model_id) for axis in envelope_raw)
    seen_axes = set()
    for axis in envelope:
        if axis.quantity in seen_axes:
            raise ModelSyntaxError(
                model_id, "BAD_ENVELOPE",
                f"axis {axis.quantity!r} is declared twice; which one bounds a "
                "query would depend on iteration order")
        seen_axes.add(axis.quantity)

    digest = entry.get("sha256")
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
        raise ModelSyntaxError(model_id, "BAD_DIGEST",
                               "'sha256' must be a 64-character hex digest")

    supersedes = entry.get("supersedes", ())
    if isinstance(supersedes, str):
        supersedes = (supersedes,)
    if not isinstance(supersedes, Sequence):
        raise ModelSyntaxError(model_id, "BAD_SUPERSEDES",
                               "'supersedes' must be a string or an array")

    return VendorModel(
        id=model_id, kind=kind, path=path, authority=authority,
        provenance=provenance, envelope=envelope,
        digest=digest.lower() if isinstance(digest, str) else None,
        supersedes=tuple(str(s) for s in supersedes),
        notes=str(entry.get("notes", "") or ""),
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

@dataclass
class Registry:
    models: Dict[str, VendorModel] = field(default_factory=dict)
    errors: List[dict] = field(default_factory=list)

    def get(self, model_id: str) -> Optional[VendorModel]:
        return self.models.get(model_id)

    def superseded_by(self) -> Dict[str, List[str]]:
        """old_id -> [ids that declare they replace it]."""
        index: Dict[str, List[str]] = {}
        for model in self.models.values():
            for old in model.supersedes:
                index.setdefault(old, []).append(model.id)
        return index


def load_registry(entries: Iterable[Any]) -> Registry:
    """Collect, never raise on one bad entry.

    One malformed model must not hide the state of the other forty; the
    errors are reported and still make the run RED, because a model that does
    not load is a model nothing can account for.
    """
    registry = Registry()
    for entry in entries:
        try:
            model = load_model(entry)
        except ModelSyntaxError as exc:
            registry.errors.append(exc.to_dict())
            continue
        if model.id in registry.models:
            registry.errors.append({
                "model": model.id, "issue": "DUPLICATE_ID",
                "detail": "two models share one id; a report keyed by id "
                          "would silently drop one, and which one survives "
                          "would depend on file order"})
            continue
        registry.models[model.id] = model
    return registry


# --------------------------------------------------------------------------- #
# Integrity: is the file still the one that was vetted?
# --------------------------------------------------------------------------- #

@dataclass
class Integrity:
    model: VendorModel
    present: bool
    digest_on_disk: Optional[str]
    status: str                    # PASS / FAIL / NO_COVERAGE
    reason: Optional[str]
    detail: str

    def to_dict(self) -> dict:
        payload = {"model": self.model.id, "path": self.model.path,
                   "present": self.present, "status": self.status,
                   "detail": self.detail}
        if self.digest_on_disk:
            payload["sha256_on_disk"] = self.digest_on_disk
        if self.reason:
            payload["issue"] = self.reason
        return payload


def check_integrity(model: VendorModel, root: Path) -> Integrity:
    """A registered model whose bytes moved is not a smaller problem than a
    missing one - it is a larger one, because it still reads as vetted."""
    from .findings import FAIL, NO_COVERAGE, PASS

    resolved = Path(model.path)
    if not resolved.is_absolute():
        resolved = root / resolved
    if not resolved.is_file():
        return Integrity(model, False, None, FAIL, MODEL_ABSENT,
                         f"{model.path!r} is registered and not on disk; every "
                         "result that names this model is unreproducible")
    digest = sha256_file(resolved)
    if model.digest is None:
        return Integrity(
            model, True, digest, NO_COVERAGE, DIGEST_UNVERIFIED,
            "the entry carries no sha256, so the file on disk cannot be tied "
            "to the one that was vetted. Register the digest printed here")
    if digest != model.digest:
        return Integrity(
            model, True, digest, FAIL, STALE_MODEL,
            f"the file changed since registration (registered "
            f"{model.digest[:12]}..., on disk {digest[:12]}...). A model "
            "swapped under a registered id keeps the authority of the one it "
            "replaced")
    return Integrity(model, True, digest, PASS, None,
                     "the file on disk is byte-identical to the registered one")


# --------------------------------------------------------------------------- #
# Envelope cross-check: does the file support what the entry claims?
# --------------------------------------------------------------------------- #

@dataclass
class EnvelopeCheck:
    model: VendorModel
    quantity: str
    status: str
    reason: Optional[str]
    detail: str
    declared: Optional[Tuple[float, float]] = None
    measured: Optional[Tuple[float, float]] = None

    def to_dict(self) -> dict:
        payload = {"model": self.model.id, "quantity": self.quantity,
                   "status": self.status, "detail": self.detail}
        if self.reason:
            payload["issue"] = self.reason
        if self.declared:
            payload["declared"] = {"min": self.declared[0],
                                   "max": self.declared[1]}
        if self.measured:
            payload["measured"] = {"min": self.measured[0],
                                   "max": self.measured[1]}
        return payload


def touchstone_frequency_span(path: Path) -> Tuple[float, float]:
    """(min_hz, max_hz) of the file's own frequency axis.

    Delegates to ``common.tline.read_touchstone`` rather than parsing the
    header here. A second Touchstone reader in this repo would be the #54
    failure repeated on purpose.
    """
    from .tline import read_touchstone

    network = read_touchstone(path)
    frequencies = network.f
    return float(min(frequencies)), float(max(frequencies))


def check_envelope(model: VendorModel, root: Path) -> List[EnvelopeCheck]:
    """Compare the DECLARED range against what the file actually contains.

    Only meaningful for kinds whose range is readable from the file itself.
    For everything else the honest answer is that the declaration was not
    checked - reported as ``ENVELOPE_UNCHECKED`` -> NO_COVERAGE, never as a
    pass. An unverifiable claim that reports PASS is how a registry becomes
    decoration.
    """
    from .findings import FAIL, NO_COVERAGE, PASS

    axis = model.axis("frequency")
    if model.kind != KIND_TOUCHSTONE or axis is None:
        return [EnvelopeCheck(
            model, axis.quantity if axis else "-", NO_COVERAGE,
            ENVELOPE_UNCHECKED,
            f"a {model.kind!r} model's range is not readable from the file, so "
            "the declaration stands unverified. It is not thereby wrong - it "
            "is unchecked")]

    resolved = Path(model.path)
    if not resolved.is_absolute():
        resolved = root / resolved
    if not resolved.is_file():
        return [EnvelopeCheck(model, axis.quantity, NO_COVERAGE,
                              MODEL_ABSENT,
                              "the file is not on disk; nothing to compare "
                              "the declaration against")]

    scale = _to_hz(axis.unit)
    if scale is None:
        return [EnvelopeCheck(
            model, axis.quantity, NO_COVERAGE, ENVELOPE_UNCHECKED,
            f"the frequency axis is declared in {axis.unit!r}, which this "
            "cross-check does not know how to convert to Hz. Guessing the "
            "factor is how a 1000x error becomes invisible")]

    try:
        low_hz, high_hz = touchstone_frequency_span(resolved)
    except Exception as exc:                      # skrf missing, or a bad file
        return [EnvelopeCheck(
            model, axis.quantity, NO_COVERAGE, ENVELOPE_UNCHECKED,
            f"the file could not be read ({type(exc).__name__}: {exc}). The "
            "declaration is unverified, which is not the same as satisfied")]

    declared = (axis.minimum * scale, axis.maximum * scale)
    measured = (low_hz, high_hz)
    # A relative tolerance, because a Touchstone axis is decimal text and the
    # round trip is not bit-exact. It is deliberately tiny: this check exists
    # to catch a 2x overclaim, not to forgive one.
    slack = 1e-9 * max(abs(declared[1]), abs(measured[1]), 1.0)
    if declared[0] < measured[0] - slack or declared[1] > measured[1] + slack:
        return [EnvelopeCheck(
            model, axis.quantity, FAIL, ENVELOPE_OVERCLAIMS,
            f"the entry claims {declared[0]:.6g}..{declared[1]:.6g} Hz, the "
            f"file holds {measured[0]:.6g}..{measured[1]:.6g} Hz. Every query "
            "in the gap would be answered by interpolation off the end of the "
            "data, and no reader in this bundle raises when that happens",
            declared, measured)]
    return [EnvelopeCheck(
        model, axis.quantity, PASS, None,
        f"the declared {declared[0]:.6g}..{declared[1]:.6g} Hz lies inside the "
        f"file's own {measured[0]:.6g}..{measured[1]:.6g} Hz",
        declared, measured)]


_HZ_SCALE = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12}


def _to_hz(unit: str) -> Optional[float]:
    return _HZ_SCALE.get(unit.strip().lower())


# --------------------------------------------------------------------------- #
# Query: may this model be used HERE?
# --------------------------------------------------------------------------- #

@dataclass
class Query:
    model_id: str
    use: str = USE_SCREENING
    at: Dict[str, float] = field(default_factory=dict)   # quantity -> value

    def to_dict(self) -> dict:
        return {"model": self.model_id, "use": self.use, "at": dict(self.at)}


@dataclass
class Answer:
    query: Query
    status: str                      # PASS / FAIL / NO_COVERAGE
    grant: str                       # signoff / screen_only / none
    reason: Optional[str]
    detail: str
    model: Optional[VendorModel] = None

    def to_dict(self) -> dict:
        payload = {"query": self.query.to_dict(), "status": self.status,
                   "grant": self.grant, "detail": self.detail}
        if self.reason:
            payload["issue"] = self.reason
        if self.model:
            payload["authority"] = self.model.authority
            payload["provenance"] = dict(self.model.provenance)
        return payload


def load_query(raw: Any) -> Query:
    if not isinstance(raw, Mapping):
        raise ModelSyntaxError("<query>", "NOT_AN_OBJECT",
                               "each query must be a JSON object")
    model_id = raw.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise ModelSyntaxError("<query>", "MISSING_FIELD",
                               "a query needs a 'model'")
    use = raw.get("use", USE_SCREENING)
    if use not in USES:
        raise ModelSyntaxError(model_id, "BAD_USE",
                               f"use must be one of {list(USES)}, got {use!r}")
    at_raw = raw.get("at", {})
    if not isinstance(at_raw, Mapping):
        raise ModelSyntaxError(model_id, "BAD_QUERY", "'at' must be an object")
    at: Dict[str, float] = {}
    for quantity, value in at_raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ModelSyntaxError(model_id, "BAD_QUERY",
                                   f"'at.{quantity}' must be a number")
        at[str(quantity)] = float(value)
    return Query(model_id=model_id.strip(), use=use, at=at)


def answer(query: Query, registry: Registry,
           integrity: Mapping[str, Integrity] | None = None) -> Answer:
    """Settle one query. Every refusal names which of the four gates it hit.

    The order matters and is not arbitrary: an unknown or unreadable model is
    settled before its envelope, because an envelope question about a file
    nobody can produce is not a question about anything.
    """
    from .findings import FAIL, NO_COVERAGE, PASS

    model = registry.get(query.model_id)
    if model is None:
        return Answer(query, FAIL, GRANT_NONE, UNKNOWN_MODEL,
                      f"no model is registered as {query.model_id!r}; a "
                      "simulation naming an unregistered model cannot be "
                      "traced back to a vendor document")

    # 1. Is the file the one that was vetted?
    if integrity is not None:
        state = integrity.get(model.id)
        if state is not None and state.status == FAIL:
            return Answer(query, FAIL, GRANT_NONE, state.reason, state.detail,
                          model)
        if state is not None and state.status == NO_COVERAGE:
            return Answer(query, NO_COVERAGE, GRANT_NONE, state.reason,
                          state.detail, model)

    # 2. Has it been replaced?
    replacements = registry.superseded_by().get(model.id, [])
    if replacements:
        return Answer(
            query, FAIL, GRANT_NONE, SUPERSEDED,
            f"{model.id!r} is superseded by {replacements}. A superseded model "
            "still produces numbers, which is exactly why using one has to be "
            "a refusal rather than a note", model)

    # 3. Is the query inside the declared envelope?
    for quantity, value in sorted(query.at.items()):
        axis = model.axis(quantity)
        if axis is None:
            return Answer(
                query, NO_COVERAGE, GRANT_NONE, AXIS_UNDECLARED,
                f"the query asks about {quantity!r} and the model declares no "
                f"range for it. The model may well be valid there; nothing "
                "here knows that, and assuming it is how an extrapolation "
                "becomes a result", model)
        if not axis.contains(value):
            return Answer(
                query, NO_COVERAGE, GRANT_NONE, OUT_OF_ENVELOPE,
                f"{quantity} = {value:g} {axis.unit} is outside the declared "
                f"{axis.minimum:g}..{axis.maximum:g} {axis.unit}. No number is "
                "returned: every reader in this bundle would have interpolated "
                "off the end of the data and none of them raise", model)

    # 4. Is the authority enough for what is being asked?
    if query.use == USE_SIGNOFF and not model.may_signoff():
        return Answer(
            query, FAIL, GRANT_NONE, AUTHORITY_INSUFFICIENT,
            f"{model.id!r} is {model.authority!r} and sign-off was requested. "
            "It can produce a real result; it cannot close a gate, because "
            "the fit (or the fiction) is ours and the vendor never validated "
            "it", model)

    grant = GRANT_SIGNOFF if model.may_signoff() else GRANT_SCREEN_ONLY
    return Answer(
        query, PASS, grant,
        None,
        f"{model.id!r} ({model.authority}) covers this query; source "
        f"{model.provenance['vendor']} {model.provenance['document']} rev "
        f"{model.provenance['revision']}, read {model.provenance['accessed']}",
        model)


# --------------------------------------------------------------------------- #
# Roll-up
# --------------------------------------------------------------------------- #

def grant_summary(answers: Sequence[Answer]) -> Dict[str, int]:
    """Clearances counted by what they GRANT.

    A run that cleared four screening queries and no sign-off must not read
    like one that cleared four sign-offs, and a single PASS count makes those
    identical - the same reason ``assertions.establishment_summary`` exists.
    """
    from .findings import PASS

    counts = {GRANT_SIGNOFF: 0, GRANT_SCREEN_ONLY: 0}
    for item in answers:
        if item.status == PASS and item.grant in counts:
            counts[item.grant] += 1
    return counts


def rollup(answers: Sequence[Answer]) -> Tuple[str, List[str]]:
    """RED > NO_COVERAGE > SCREEN_ONLY > GREEN.

    SCREEN_ONLY appears when everything cleared but nothing cleared for
    sign-off: the run is real and it did not open a gate.
    """
    from .findings import FAIL, NO_COVERAGE

    failures = [a for a in answers if a.status == FAIL]
    if failures:
        return RED, sorted({a.reason or a.query.model_id for a in failures})
    uncovered = [a for a in answers if a.status == NO_COVERAGE]
    if uncovered:
        return NO_COVERAGE, sorted({a.reason or OUT_OF_ENVELOPE
                                    for a in uncovered})
    if not answers:
        return NO_COVERAGE, ["NO_QUERIES"]
    counts = grant_summary(answers)
    if counts[GRANT_SIGNOFF] == 0:
        return SCREEN_ONLY, ["NO_SIGNOFF_GRADE_MODEL"]
    return GREEN, []
