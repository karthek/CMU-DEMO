"""Provider-neutral ports only. Concrete API adapters belong to later phases."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Protocol, TypeVar
from travel_agent.live.observations import Provenance, airport, number, text
from travel_agent.live.time import utc
from travel_agent.live.mail import MailMessage


class ProviderErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"


@dataclass(frozen=True)
class ProviderError:
    code: ProviderErrorCode
    retryable: bool
    retry_after_seconds: int | None = None

    def __post_init__(self):
        if not isinstance(self.code, ProviderErrorCode) or type(self.retryable) is not bool:
            raise ValueError("Typed provider error required")
        if self.retry_after_seconds is not None and (type(self.retry_after_seconds) is not int or self.retry_after_seconds < 0):
            raise ValueError("Non-negative integer retry delay required")


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    value: T | None = None
    error: ProviderError | None = None

    def __post_init__(self):
        if (self.value is None) == (self.error is None):
            raise ValueError("Exactly one provider value or error required")
        if self.error is not None and not isinstance(self.error, ProviderError):
            raise ValueError("Typed provider error required")


@dataclass(frozen=True)
class MailSyncPage:
    messages: tuple[MailMessage, ...]
    removed_message_ids: tuple[str, ...]
    next_page_token: str | None
    completed_cursor: str | None

    def __post_init__(self):
        if self.next_page_token is not None and self.completed_cursor is not None:
            raise ValueError("Cannot commit synchronization cursor before last page")


class MailSource(Protocol):
    """Adapter-owned opaque cursors; pages are deltas, never complete V8 snapshots.

    A completed cursor is eligible for persistence only after every page and its
    processing results commit. Cursor expiration requires explicit resync.
    """
    def sync(self, *, since: datetime, cursor: str | None,
             page_token: str | None = None) -> ProviderResult[MailSyncPage]: ...


@dataclass(frozen=True)
class FlightIdentity:
    segment_id: str
    flight_number: str
    origin: str
    destination: str
    scheduled_departure: datetime
    origin_timezone: str

    def __post_init__(self):
        from zoneinfo import ZoneInfo
        text(self.segment_id)
        text(self.flight_number)
        airport(self.origin)
        airport(self.destination)
        ZoneInfo(self.origin_timezone)
        object.__setattr__(self, "scheduled_departure", utc(self.scheduled_departure))


@dataclass(frozen=True)
class FlightObservation:
    identity: FlightIdentity
    departure: datetime
    cancelled: bool
    terminal: str | None
    gate: str | None
    provenance: Provenance

    def __post_init__(self):
        object.__setattr__(self, "departure", utc(self.departure))
        if type(self.cancelled) is not bool:
            raise ValueError("Boolean cancellation required")
        for value in (self.terminal, self.gate):
            if value is not None:
                text(value)


class FlightProvider(Protocol):
    def get_flight(self, identity: FlightIdentity) -> ProviderResult[FlightObservation]: ...


@dataclass(frozen=True)
class TrafficRequest:
    origin: str
    airport: str
    departure_time: datetime

    def __post_init__(self):
        text(self.origin)
        airport(self.airport)
        object.__setattr__(self, "departure_time", utc(self.departure_time))


@dataclass(frozen=True)
class TrafficEstimate:
    request: TrafficRequest
    road_minutes: float
    provenance: Provenance

    def __post_init__(self):
        number(self.road_minutes)


class TrafficProvider(Protocol):
    def estimate(self, request: TrafficRequest) -> ProviderResult[TrafficEstimate]: ...


@dataclass(frozen=True)
class CalendarAuthority:
    is_organizer: bool
    can_reschedule: bool
    can_cancel: bool
    can_decline: bool

    def __post_init__(self):
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError("Boolean permission evidence required")


@dataclass(frozen=True)
class CalendarEvent:
    account_id: str
    calendar_id: str
    event_id: str
    version: str
    start: datetime
    end: datetime
    authority: CalendarAuthority

    def __post_init__(self):
        for value in (self.account_id, self.calendar_id, self.event_id, self.version):
            text(value)
        object.__setattr__(self, "start", utc(self.start))
        object.__setattr__(self, "end", utc(self.end))
        if self.end <= self.start:
            raise ValueError("Calendar event must have positive duration")


class CalendarProvider(Protocol):
    def list_events(self, *, start: datetime, end: datetime) -> ProviderResult[tuple[CalendarEvent, ...]]: ...
    def get_event(self, calendar_id: str, event_id: str) -> ProviderResult[CalendarEvent]: ...
    # Write port is intentionally deferred until server-side approval/claim contracts exist.
