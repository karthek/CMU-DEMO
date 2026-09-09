"""Immutable internal results, serialized only at application boundaries."""
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum


class ActivationStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_YET_ELIGIBLE = "NOT_YET_ELIGIBLE"
    DEPARTED = "DEPARTED"
    CANCELLED = "CANCELLED"
    INVALID_SEGMENT = "INVALID_SEGMENT"


class ExecutionState(StrEnum):
    NOT_ACTIVATED = "NOT_ACTIVATED"
    PLANNING = "PLANNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Trigger(StrEnum):
    AUTOMATIC_LEAD_TIME = "AUTOMATIC_LEAD_TIME"
    USER_REQUEST = "USER_REQUEST"


class Serializable:
    def to_dict(self):
        def wire(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {k: wire(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [wire(v) for v in value]
            return value
        return wire(asdict(self))


@dataclass(frozen=True)
class BookedSegment(Serializable):
    itinerary_id: str
    segment_id: str
    source_segment_id: str
    flight_number: str
    departure_date: str
    origin: str
    destination: str
    scheduled_departure: datetime
    booking_status: str


@dataclass(frozen=True)
class BookedItinerary(Serializable):
    itinerary_id: str
    source_id: str
    source_itinerary_id: str
    segments: tuple[BookedSegment, ...]


@dataclass(frozen=True)
class Diagnostic(Serializable):
    code: str
    message: str
    record_index: int | None = None
    itinerary_id: str | None = None
    segment_id: str | None = None
    field: str | None = None


@dataclass(frozen=True)
class NormalizationResult(Serializable):
    source_id: str
    status: str
    records_retrieved: int
    itineraries: tuple[BookedItinerary, ...]
    duplicates_ignored: int
    invalid_records: int
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class ActivationResult(Serializable):
    itinerary_id: str | None
    segment_id: str | None
    status: ActivationStatus
    eligible: bool
    as_of: datetime
    scheduled_departure: datetime | None
    activation_time: datetime | None
    planning_lead_time_minutes: int
    time_until_departure_minutes: float | None
    reason_code: str
    evidence: dict


@dataclass(frozen=True)
class RefreshResult(Serializable):
    status: str
    source_id: str
    as_of: datetime
    records_retrieved: int
    itineraries_created: int
    itineraries_updated: int
    itineraries_unchanged: int
    duplicates_ignored: int
    invalid_records: int
    itineraries_marked_missing: int
    upcoming_segments: tuple[BookedSegment, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class PlanningProvenance(Serializable):
    activation_mode: str
    trigger: str
    as_of: datetime
    itinerary_id: str
    segment_id: str
    itinerary_revision: int


@dataclass(frozen=True)
class PlanningAttempt(Serializable):
    attempt_number: int
    activation_mode: str
    trigger: str
    itinerary_revision: int
    decision_as_of: str
    started_at: str
    finished_at: str | None
    state: str
    activation_result: dict
    provenance: dict
    context: dict | None
    planning_result: dict | None
    error: dict | None


@dataclass(frozen=True)
class AutomaticDispatch(Serializable):
    itinerary_id: str
    segment_id: str
    decision: str
    automatic_state_before: str
    automatic_state_after: str
    current_itinerary_revision: int
    activation_result: dict
    attempt: dict | None


@dataclass(frozen=True)
class MonitorResult(Serializable):
    as_of: datetime
    time_basis: str
    refresh: dict | None
    activation_results: tuple[dict, ...]
    dispatches: tuple[AutomaticDispatch, ...]
    activated_segments: tuple[BookedSegment, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class ManualResult(Serializable):
    as_of: datetime
    time_basis: str
    refresh: dict | None
    status: str
    matches: tuple[BookedSegment, ...]
    run: dict | None
    diagnostics: tuple[Diagnostic, ...]
