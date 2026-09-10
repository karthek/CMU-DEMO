"""Typed observation contracts. Provenance is evidence, never authorization."""
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
import math
import re
from typing import Generic, TypeVar
from travel_agent.live.time import parse_instant, utc


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Nonempty text required")


def number(value, *, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        raise ValueError("Finite non-negative number required")


def airport(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Normalized airport code required")


class RetrievedBy(StrEnum):
    HOST = "HOST"
    PROVIDER = "PROVIDER"
    SIMULATED = "SIMULATED"


class ObservationTable(StrEnum):
    SECURITY = "security_observations"
    PARKING = "parking_observations"
    RIDESHARE = "rideshare_observations"
    LOCATION = "location_observations"


class ParkingCategory(StrEnum):
    ECONOMY = "ECONOMY"
    DAILY = "DAILY"
    TERMINAL_GARAGE = "TERMINAL/GARAGE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Provenance:
    source: str
    observed_at: datetime
    retrieved_by: RetrievedBy

    def __post_init__(self):
        text(self.source)
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        if not isinstance(self.retrieved_by, RetrievedBy):
            raise ValueError("Typed retrieved_by required")


@dataclass(frozen=True)
class HostObservation:
    segment_id: str | None
    provenance: Provenance

    def __post_init__(self):
        if self.segment_id is not None:
            text(self.segment_id)
        if not isinstance(self.provenance, Provenance) or self.provenance.retrieved_by != RetrievedBy.HOST:
            raise ValueError("Host provenance required")


@dataclass(frozen=True)
class SecurityObservation(HostObservation):
    airport: str
    screening_type: str
    wait_minutes: float
    terminal: str | None = None
    checkpoint: str | None = None

    def __post_init__(self):
        super().__post_init__()
        text(self.segment_id)
        airport(self.airport)
        text(self.screening_type)
        number(self.wait_minutes)
        for value in (self.terminal, self.checkpoint):
            if value is not None:
                text(value)


@dataclass(frozen=True)
class ParkingObservation(HostObservation):
    airport: str
    category: ParkingCategory
    availability: Availability
    parking_to_terminal_minutes: float
    price: float | None = None
    currency: str | None = None

    def __post_init__(self):
        super().__post_init__()
        text(self.segment_id)
        airport(self.airport)
        if not isinstance(self.category, ParkingCategory) or not isinstance(self.availability, Availability):
            raise ValueError("Unsupported parking category/availability")
        number(self.parking_to_terminal_minutes)
        money(self.price, self.currency)


def money(value, currency):
    if value is None:
        if currency is not None:
            raise ValueError("Currency without price")
    else:
        number(value)
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Currency code required with price")


@dataclass(frozen=True)
class RideshareObservation(HostObservation):
    origin: str
    airport: str
    pickup_eta_minutes: float
    travel_minutes: float
    estimated_fare: float | None = None
    currency: str | None = None

    def __post_init__(self):
        super().__post_init__()
        text(self.segment_id)
        text(self.origin)
        airport(self.airport)
        number(self.pickup_eta_minutes)
        number(self.travel_minutes)
        money(self.estimated_fare, self.currency)


@dataclass(frozen=True)
class LocationObservation(HostObservation):
    latitude: float
    longitude: float
    precision_meters: float
    permission_reference: str

    def __post_init__(self):
        super().__post_init__()
        for value, limit in ((self.latitude, 90), (self.longitude, 180)):
            if type(value) not in (float, int) or not math.isfinite(value) or abs(value) > limit:
                raise ValueError("Invalid coordinate")
        number(self.precision_meters)
        text(self.permission_reference)


HOST_ROW_TYPES = {ObservationTable.SECURITY: SecurityObservation, ObservationTable.PARKING: ParkingObservation,
                 ObservationTable.RIDESHARE: RideshareObservation, ObservationTable.LOCATION: LocationObservation}


def parse_host_row(table: ObservationTable, row: dict, *, as_of: datetime, segment_id: str, airport_code: str,
                   authorized_location_permissions: frozenset[str] = frozenset()) -> HostObservation:
    """Closed flat table schema -> typed row. Authority references come from core/session, not the row."""
    utc(as_of)
    text(segment_id)
    airport(airport_code)
    if not isinstance(table, ObservationTable) or not isinstance(row, dict):
        raise ValueError("Typed table and object row required")
    cls = HOST_ROW_TYPES[table]
    allowed = {f.name for f in fields(cls)} - {"provenance"} | {"source", "observed_at", "retrieved_by"}
    if set(row) - allowed:
        raise ValueError("Unknown observation fields")
    try:
        data = dict(row)
        provenance = Provenance(data.pop("source"), parse_instant(data.pop("observed_at")), RetrievedBy(data.pop("retrieved_by")))
        if table == ObservationTable.PARKING:
            data["category"] = ParkingCategory(data["category"])
            data["availability"] = Availability(data["availability"])
        result = cls(provenance=provenance, **data)
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError("Invalid host observation") from exc
    if result.provenance.observed_at > utc(as_of):
        raise ValueError("Future observation")
    if result.segment_id != segment_id and not (table == ObservationTable.LOCATION and result.segment_id is None):
        raise ValueError("Observation segment mismatch")
    if getattr(result, "airport", airport_code) != airport_code:
        raise ValueError("Observation airport mismatch")
    if isinstance(result, LocationObservation) and result.permission_reference not in authorized_location_permissions:
        raise ValueError("Location permission not authorized by session")
    return result


T = TypeVar("T")


@dataclass(frozen=True)
class LiveObservation(Generic[T]):
    observation_id: str
    segment_id: str
    value: T
    provenance: Provenance

    def __post_init__(self):
        text(self.observation_id)
        text(self.segment_id)
        if self.value is None or not isinstance(self.provenance, Provenance):
            raise ValueError("Value and provenance required")


@dataclass(frozen=True)
class FreshnessPolicy:
    max_age_minutes: int = 60

    def __post_init__(self):
        if type(self.max_age_minutes) is not int or self.max_age_minutes < 0:
            raise ValueError("Non-negative integer freshness required")


DEFAULT_FRESHNESS_POLICY = FreshnessPolicy()


class DataStatus(StrEnum):
    USABLE = "USABLE"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


@dataclass(frozen=True)
class FreshnessResult:
    status: DataStatus
    age_minutes: float | None
    reason: str
    retrieval_failure_code: str | None


def freshness(observation: LiveObservation | HostObservation | None, as_of: datetime,
              policy=DEFAULT_FRESHNESS_POLICY, *, retrieval_failure_code: str | None = None) -> FreshnessResult:
    now = utc(as_of)
    if observation is None:
        return FreshnessResult(DataStatus.DATA_UNAVAILABLE, None, "MISSING", retrieval_failure_code)
    age = (now - observation.provenance.observed_at).total_seconds() / 60
    reason = ("SIMULATED_NOT_LIVE" if observation.provenance.retrieved_by == RetrievedBy.SIMULATED else
              "FUTURE" if age < 0 else "STALE" if age > policy.max_age_minutes else "FRESH")
    return FreshnessResult(DataStatus.USABLE if reason == "FRESH" else DataStatus.DATA_UNAVAILABLE,
                           age, reason, retrieval_failure_code)


@dataclass(frozen=True)
class HostInputRequired:
    segment_id: str
    required_tables: tuple[ObservationTable, ...]
    optional_tables: tuple[ObservationTable, ...]
    freshness_minutes: int
    reason: str = "HOST_INPUT_REQUIRED"


def required_host_input(segment_id: str, required: frozenset[ObservationTable], optional: frozenset[ObservationTable],
                        available: dict[ObservationTable, HostObservation], *, as_of: datetime,
                        policy=DEFAULT_FRESHNESS_POLICY) -> HostInputRequired | None:
    text(segment_id)
    utc(as_of)
    if required & optional:
        raise ValueError("Required and optional tables overlap")
    if any(not isinstance(table, ObservationTable) for table in required | optional):
        raise ValueError("Typed observation tables required")
    for table, observation in available.items():
        if not isinstance(table, ObservationTable) or not isinstance(observation, HOST_ROW_TYPES[table]):
            raise ValueError("Observation type does not match table")
    missing = tuple(sorted(t for t in required if t not in available or
                           available[t].segment_id not in (segment_id, None) or
                           freshness(available[t], as_of, policy).status != DataStatus.USABLE))
    return HostInputRequired(segment_id, missing, tuple(sorted(optional)), policy.max_age_minutes) if missing else None
