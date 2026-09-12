"""V9 normalized calendar reads and deterministic travel-window constraints.

No provider objects, recurrence expansion, transportation timing or write authority.
The earlier live.providers calendar scaffold and V8 models remain unchanged.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from hashlib import sha256
import json
from typing import Protocol
from zoneinfo import ZoneInfo

from travel_agent.live.observations import Provenance, text
from travel_agent.live.providers import ProviderResult
from travel_agent.live.time import from_local, utc


def _text(value, maximum=1024):
    text(value)
    if len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("Bounded single-line calendar field required")


@dataclass(frozen=True, order=True)
class CalendarScope:
    provider: str
    account_id: str
    calendar_id: str

    def __post_init__(self):
        for value in (self.provider, self.account_id, self.calendar_id):
            _text(value)


@dataclass(frozen=True)
class TimeInterval:
    """Half-open aware interval, compared and subtracted only in UTC."""
    start: datetime
    end: datetime

    def __post_init__(self):
        object.__setattr__(self, "start", utc(self.start))
        object.__setattr__(self, "end", utc(self.end))
        if self.end <= self.start:
            raise ValueError("Positive interval required")

    def overlap(self, other):
        start, end = max(self.start, other.start), min(self.end, other.end)
        return TimeInterval(start, end) if start < end else None


@dataclass(frozen=True)
class AllDaySpan:
    """Civil dates, exclusive end, never a midnight meeting or fixed 24 hours."""
    start_date: date
    end_date: date
    source_timezone: str | None

    def __post_init__(self):
        if type(self.start_date) is not date or type(self.end_date) is not date or self.end_date <= self.start_date:
            raise ValueError("Positive exclusive civil-date span required")
        if self.source_timezone is not None:
            _text(self.source_timezone)
            ZoneInfo(self.source_timezone)

    def interval(self):
        if self.source_timezone is None:
            return None
        # Gaps/folds at midnight are rejected rather than assigned a guessed offset.
        return TimeInterval(from_local(datetime.combine(self.start_date, time.min), self.source_timezone),
                            from_local(datetime.combine(self.end_date, time.min), self.source_timezone))


class EventKind(StrEnum):
    SINGLE = "SINGLE"
    SERIES_MASTER = "SERIES_MASTER"
    OCCURRENCE = "OCCURRENCE"
    EXCEPTION = "EXCEPTION"


@dataclass(frozen=True)
class CalendarEventIdentity:
    scope: CalendarScope
    event_id: str
    kind: EventKind = EventKind.SINGLE
    series_id: str | None = None
    occurrence_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.scope, CalendarScope) or not isinstance(self.kind, EventKind):
            raise ValueError("Typed scope/event kind required")
        _text(self.event_id)
        recurring = self.kind in (EventKind.OCCURRENCE, EventKind.EXCEPTION)
        if recurring:
            _text(self.series_id)
            _text(self.occurrence_id)
        elif self.series_id is not None or self.occurrence_id is not None:
            raise ValueError("Only expanded occurrences carry series and occurrence keys")

    @property
    def key(self):
        scope = (self.scope.provider, self.scope.account_id, self.scope.calendar_id)
        if self.kind in (EventKind.OCCURRENCE, EventKind.EXCEPTION):
            return (*scope, "occurrence", self.series_id, self.occurrence_id)
        return (*scope, self.kind.value, self.event_id)


class EventStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    TENTATIVE = "TENTATIVE"
    CANCELLED = "CANCELLED"
    REMOVED = "REMOVED"
    UNKNOWN = "UNKNOWN"


class CalendarAvailability(StrEnum):
    BUSY = "BUSY"
    FREE = "FREE"
    TENTATIVE = "TENTATIVE"
    UNKNOWN = "UNKNOWN"


class TravelerRole(StrEnum):
    ORGANIZER = "ORGANIZER"
    ATTENDEE = "ATTENDEE"
    NON_PARTICIPANT = "NON_PARTICIPANT"
    UNKNOWN = "UNKNOWN"


class Participation(StrEnum):
    ACCEPTED = "ACCEPTED"
    TENTATIVE = "TENTATIVE"
    DECLINED = "DECLINED"
    NEEDS_ACTION = "NEEDS_ACTION"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NormalizedCalendarEvent:
    identity: CalendarEventIdentity
    period: TimeInterval | AllDaySpan | None
    status: EventStatus
    availability: CalendarAvailability
    traveler_role: TravelerRole
    participation: Participation
    provenance: Provenance
    source_version: str | None = None
    source_modified_at: datetime | None = None
    source_timezone: str | None = None
    organizer_id: str | None = None
    title: str | None = None
    location: str | None = None

    def __post_init__(self):
        typed = ((self.identity, CalendarEventIdentity), (self.status, EventStatus),
                 (self.availability, CalendarAvailability), (self.traveler_role, TravelerRole),
                 (self.participation, Participation), (self.provenance, Provenance))
        if any(not isinstance(value, kind) for value, kind in typed):
            raise ValueError("Typed normalized calendar evidence required")
        if self.period is not None and not isinstance(self.period, (TimeInterval, AllDaySpan)):
            raise ValueError("Typed event period required")
        for value in (self.source_version, self.organizer_id, self.title, self.location):
            if value is not None:
                _text(value)
        if self.source_timezone is not None:
            _text(self.source_timezone)
            ZoneInfo(self.source_timezone)
        if isinstance(self.period, AllDaySpan) and self.source_timezone != self.period.source_timezone:
            raise ValueError("All-day zone metadata must agree")
        if self.source_modified_at is not None:
            modified = utc(self.source_modified_at)
            if modified > self.provenance.observed_at:
                raise ValueError("Modification cannot follow observation")
            object.__setattr__(self, "source_modified_at", modified)

    @property
    def all_day(self):
        return isinstance(self.period, AllDaySpan)

    def interval(self):
        return self.period.interval() if self.all_day else self.period


@dataclass(frozen=True)
class CalendarQuery:
    scope: CalendarScope
    window: TimeInterval
    max_events: int = 1000

    def __post_init__(self):
        if not isinstance(self.scope, CalendarScope) or not isinstance(self.window, TimeInterval):
            raise ValueError("Typed calendar scope and query interval required")
        if self.window.end - self.window.start > timedelta(days=31):
            raise ValueError("Calendar read span exceeds 31 days")
        if type(self.max_events) is not int or not 1 <= self.max_events <= 1000:
            raise ValueError("Calendar event bound must be 1..1000")


@dataclass(frozen=True)
class CalendarSnapshot:
    """A successful COMPLETE bounded read, never a delta or partial page.

    All requested pages and occurrence expansion must complete before construction.
    Unknown periods are retained conservatively; cancelled tombstones may lack time.
    """
    query: CalendarQuery
    events: tuple[NormalizedCalendarEvent, ...]
    retrieved_at: datetime

    def __post_init__(self):
        if not isinstance(self.query, CalendarQuery) or not isinstance(self.events, tuple):
            raise ValueError("Typed query and immutable event tuple required")
        object.__setattr__(self, "retrieved_at", utc(self.retrieved_at))
        if len(self.events) > self.query.max_events:
            raise ValueError("Calendar result bound exceeded")
        seen = set()
        for event in self.events:
            if not isinstance(event, NormalizedCalendarEvent) or event.identity.scope != self.query.scope:
                raise ValueError("Calendar result outside query scope")
            if event.identity.kind == EventKind.SERIES_MASTER:
                raise ValueError("Unexpanded recurring master cannot be complete query evidence")
            if event.identity.key in seen or event.provenance.observed_at > self.retrieved_at:
                raise ValueError("Duplicate identity or future event observation")
            seen.add(event.identity.key)
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda e: e.identity.key)))


class CalendarProvider(Protocol):
    def read_events(self, query: CalendarQuery) -> ProviderResult[CalendarSnapshot]:
        """Complete scoped overlapping occurrences, or neutral error; no mutations.

        No clipped event periods, unexpanded masters, partial pages, credentials or
        raw provider objects. Unknown-time evidence cannot silently be dropped.
        A success asserts coverage only for query.scope/window, not all calendars.
        """
        ...


@dataclass(frozen=True)
class CalendarPolicy:
    tentative_blocks: bool = True
    all_day_blocks_when_busy: bool = True
    max_age: timedelta = timedelta(minutes=15)

    def __post_init__(self):
        if type(self.tentative_blocks) is not bool or type(self.all_day_blocks_when_busy) is not bool:
            raise ValueError("Explicit boolean calendar policy required")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("Positive calendar freshness duration required")


class ConflictState(StrEnum):
    NO_CONFLICT = "NO_CONFLICT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


class EventEffect(StrEnum):
    IGNORED = "IGNORED"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"


class CalendarReason(StrEnum):
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    CANCELLED = "CANCELLED"
    DECLINED = "DECLINED"
    FREE = "FREE"
    NON_PARTICIPANT = "NON_PARTICIPANT"
    TENTATIVE_IGNORED = "TENTATIVE_IGNORED"
    ALL_DAY_IGNORED = "ALL_DAY_IGNORED"
    BUSY_COMMITMENT = "BUSY_COMMITMENT"
    TENTATIVE_COMMITMENT = "TENTATIVE_COMMITMENT"
    ALL_DAY_BUSY = "ALL_DAY_BUSY"
    UNKNOWN_TIME = "UNKNOWN_TIME"
    UNKNOWN_STATUS = "UNKNOWN_STATUS"
    UNKNOWN_PARTICIPATION = "UNKNOWN_PARTICIPATION"
    UNKNOWN_AVAILABILITY = "UNKNOWN_AVAILABILITY"
    REMOVAL_UNRESOLVED = "REMOVAL_UNRESOLVED"
    STALE_OR_FUTURE_EVIDENCE = "STALE_OR_FUTURE_EVIDENCE"


@dataclass(frozen=True)
class CalendarAssessment:
    event: NormalizedCalendarEvent
    effect: EventEffect
    reason: CalendarReason
    overlap: TimeInterval | None

    @property
    def conflict_token(self):
        if self.effect != EventEffect.BLOCKING:
            return None
        interval = self.event.interval()
        facts = (self.event.identity.key, interval.start.isoformat(), interval.end.isoformat(),
                 self.overlap.start.isoformat(), self.overlap.end.isoformat(), self.reason.value)
        payload = json.dumps(facts, ensure_ascii=True, separators=(",", ":")).encode()
        return "calendar-conflict/v1:sha256:" + sha256(payload).hexdigest()


@dataclass(frozen=True)
class CalendarConflictResult:
    query: CalendarQuery
    travel_window: TimeInterval
    policy: CalendarPolicy
    state: ConflictState
    evidence: tuple[CalendarAssessment, ...]
    issues: tuple[CalendarReason, ...] = ()

    @property
    def reliable(self):
        return not self.issues and all(e.effect != EventEffect.UNKNOWN for e in self.evidence)

    @property
    def conflict_tokens(self):
        return frozenset(e.conflict_token for e in self.evidence if e.effect == EventEffect.BLOCKING)


class CalendarConflictEvaluator:
    def evaluate(self, snapshot: CalendarSnapshot, travel_window: TimeInterval, *, as_of: datetime,
                 policy=CalendarPolicy()) -> CalendarConflictResult:
        if not isinstance(snapshot, CalendarSnapshot) or not isinstance(travel_window, TimeInterval) or not isinstance(policy, CalendarPolicy):
            raise ValueError("Typed calendar evaluation inputs required")
        as_of = utc(as_of)
        query = snapshot.query
        if query.window.start > travel_window.start or query.window.end < travel_window.end:
            raise ValueError("Calendar coverage must contain the complete travel window")
        observations = (snapshot.retrieved_at, *(e.provenance.observed_at for e in snapshot.events))
        if any(stamp > as_of or as_of - stamp > policy.max_age for stamp in observations):
            return CalendarConflictResult(query, travel_window, policy, ConflictState.UNKNOWN, (),
                                          (CalendarReason.STALE_OR_FUTURE_EVIDENCE,))
        evidence = tuple(self._assess(event, travel_window, policy) for event in snapshot.events)
        state = (ConflictState.CONFLICT if any(e.effect == EventEffect.BLOCKING for e in evidence)
                 else ConflictState.UNKNOWN if any(e.effect == EventEffect.UNKNOWN for e in evidence)
                 else ConflictState.NO_CONFLICT)
        return CalendarConflictResult(query, travel_window, policy, state, evidence)

    @staticmethod
    def _assess(event, window, policy):
        overlap = None
        def assessment(effect, reason):
            return CalendarAssessment(event, effect, reason, overlap)
        ignore, unknown, block = EventEffect.IGNORED, EventEffect.UNKNOWN, EventEffect.BLOCKING
        reason = CalendarReason
        if event.status == EventStatus.CANCELLED:
            return assessment(ignore, reason.CANCELLED)
        if event.status == EventStatus.REMOVED:
            return assessment(unknown, reason.REMOVAL_UNRESOLVED)
        try:
            interval = event.interval()
        except ValueError:
            interval = None
        if interval is not None:
            overlap = interval.overlap(window)
            if overlap is None:
                return assessment(ignore, reason.OUTSIDE_WINDOW)
        if event.availability == CalendarAvailability.FREE:
            return assessment(ignore, reason.FREE)
        if event.traveler_role == TravelerRole.NON_PARTICIPANT:
            return assessment(ignore, reason.NON_PARTICIPANT)
        if event.traveler_role == TravelerRole.ATTENDEE and event.participation == Participation.DECLINED:
            return assessment(ignore, reason.DECLINED)
        if interval is None:
            return assessment(unknown, reason.UNKNOWN_TIME)
        if event.status == EventStatus.UNKNOWN:
            return assessment(unknown, reason.UNKNOWN_STATUS)
        if (event.traveler_role == TravelerRole.UNKNOWN
            or (event.traveler_role == TravelerRole.ATTENDEE and event.participation not in (Participation.ACCEPTED, Participation.TENTATIVE))
            or (event.traveler_role == TravelerRole.ORGANIZER and event.participation in (Participation.DECLINED, Participation.NEEDS_ACTION))):
            return assessment(unknown, reason.UNKNOWN_PARTICIPATION)
        if event.availability == CalendarAvailability.UNKNOWN:
            return assessment(unknown, reason.UNKNOWN_AVAILABILITY)
        if event.all_day and not policy.all_day_blocks_when_busy:
            return assessment(ignore, reason.ALL_DAY_IGNORED)
        tentative = (event.status == EventStatus.TENTATIVE or event.availability == CalendarAvailability.TENTATIVE
                     or event.participation == Participation.TENTATIVE)
        if tentative:
            return assessment(block if policy.tentative_blocks else ignore,
                              reason.TENTATIVE_COMMITMENT if policy.tentative_blocks else reason.TENTATIVE_IGNORED)
        return assessment(block, reason.ALL_DAY_BUSY if event.all_day else reason.BUSY_COMMITMENT)
