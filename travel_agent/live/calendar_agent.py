"""Opt-in aware V9 calendar boundary; the frozen V8 CalendarAgent is unchanged."""
from dataclasses import dataclass
from datetime import datetime

from travel_agent.live.calendar import (CalendarProvider, CalendarQuery, CalendarSnapshot,
    CalendarConflictEvaluator, CalendarPolicy, CalendarScope, TimeInterval, NormalizedCalendarEvent,
    EventKind, EventStatus, CalendarConflictResult)
from travel_agent.live.providers import ProviderResult, ProviderError, ProviderErrorCode as Code
from travel_agent.live.time import utc


class CalendarReadAgent:
    """Read commitments and judge the caller's interval; never invent travel times."""
    def __init__(self, provider: CalendarProvider):
        self._provider = provider

    def evaluate_window(self, query: CalendarQuery, travel_window: TimeInterval, *, as_of: datetime,
                        policy=CalendarPolicy()) -> ProviderResult[CalendarConflictResult]:
        # Caller input mistakes fail before provider I/O.
        if not isinstance(query, CalendarQuery) or not isinstance(travel_window, TimeInterval) or not isinstance(policy, CalendarPolicy):
            raise ValueError("Typed query/window/policy required")
        as_of = utc(as_of)
        if query.window.start > travel_window.start or query.window.end < travel_window.end:
            raise ValueError("Query must cover travel window")
        try:
            result = self._provider.read_events(query)
            if not isinstance(result, ProviderResult):
                raise ValueError("Typed provider result required")
            if result.error is not None:
                return result
            snapshot = result.value
            if not isinstance(snapshot, CalendarSnapshot) or snapshot.query != query:
                raise ValueError("Complete exact-query snapshot required")
            return ProviderResult(value=CalendarConflictEvaluator().evaluate(snapshot, travel_window,
                as_of=as_of, policy=policy))
        except TimeoutError:
            return ProviderResult(error=ProviderError(Code.TIMEOUT, True))
        except Exception:
            return ProviderResult(error=ProviderError(Code.INVALID_RESPONSE, False))


@dataclass(frozen=True)
class FixtureCalendarProvider:
    """A small complete in-memory calendar at one fixed observation, not a cache."""
    scope: CalendarScope
    events: tuple[NormalizedCalendarEvent, ...]
    retrieved_at: datetime

    def __post_init__(self):
        if not isinstance(self.scope, CalendarScope) or not isinstance(self.events, tuple) or len(self.events) > 1000:
            raise ValueError("Bounded immutable fixture calendar required")
        object.__setattr__(self, "retrieved_at", utc(self.retrieved_at))
        if any(not isinstance(e, NormalizedCalendarEvent) or e.identity.scope != self.scope for e in self.events):
            raise ValueError("Fixture calendar scope mismatch")
        if len({e.identity.key for e in self.events}) != len(self.events):
            raise ValueError("Fixture must resolve duplicate identities before window filtering")

    def read_events(self, query: CalendarQuery) -> ProviderResult[CalendarSnapshot]:
        try:
            if not isinstance(query, CalendarQuery) or query.scope != self.scope:
                raise ValueError("Fixture query scope mismatch")
            selected = []
            # Never hide unresolved times, removals or unexpanded series by filtering
            # only their first/current start. Snapshot validation rejects masters.
            for event in self.events:
                try:
                    interval = event.interval()
                except ValueError:
                    interval = None
                if (event.identity.kind == EventKind.SERIES_MASTER or interval is None
                    or event.status in (EventStatus.CANCELLED, EventStatus.REMOVED)
                    or interval.overlap(query.window) is not None):
                    selected.append(event)
            return ProviderResult(value=CalendarSnapshot(query, tuple(selected), self.retrieved_at))
        except Exception:
            return ProviderResult(error=ProviderError(Code.INVALID_RESPONSE, False))
