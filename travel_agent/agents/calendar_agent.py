from datetime import datetime, time, timedelta

from functools import partial
from travel_agent.agents import _validation
from travel_agent.agents.errors import DataUnavailableError, InvalidDataError, InvalidInputError, ToolFailureError
from travel_agent.models import CalendarEvent

text = partial(_validation.text, agent="calendar")
timestamp = partial(_validation.timestamp, agent="calendar")


def _events(events):
    if not isinstance(events, list):
        raise InvalidDataError("Calendar data must be a list of events.", agent="calendar")
    normalized = []
    for event in events:
        if not isinstance(event, CalendarEvent):
            raise InvalidDataError("Calendar event has an invalid structure.", agent="calendar")
        start = timestamp(event.start, "event.start")
        end = timestamp(event.end, "event.end")
        if end <= start:
            raise InvalidDataError("Event end must be after event start.", agent="calendar")
        text(event.priority, "event.priority")
        normalized.append(CalendarEvent(
            title=text(event.title, "event.title"), start=start.isoformat(),
            end=end.isoformat(), priority=event.priority,
        ))
    return sorted(normalized, key=lambda event: timestamp(event.start, "event.start"))


class CalendarAgent:
    """Read and reason only. No write, cancellation, or rescheduling capability."""

    def __init__(self, calendar_tool):
        self._calendar_tool = calendar_tool

    def get_events(self, date: str) -> list[CalendarEvent]:
        day = _validation.travel_date(date, agent="calendar")
        start = datetime.combine(day, time.min)
        try:
            end = start + timedelta(days=1)
        except OverflowError as exc:
            raise InvalidInputError("Travel date is outside the supported calendar range.", agent="calendar") from exc
        try:
            events = self._calendar_tool.get_events(date)
        except Exception as exc:
            raise ToolFailureError("Calendar retrieval failed.", agent="calendar") from exc
        if events is None:
            raise DataUnavailableError("Calendar information is unavailable.", agent="calendar")
        # Include meetings overlapping the requested day, including overnight meetings.
        return [event for event in _events(events)
                if timestamp(event.start, "event.start") < end
                and timestamp(event.end, "event.end") > start]

    def last_meeting_end(self, events: list[CalendarEvent]) -> str | None:
        events = _events(events)
        if not events:
            return None
        return max(timestamp(event.end, "event.end") for event in events).isoformat()

    def departure_conflicts(self, events: list[CalendarEvent], departure_time: str) -> list[CalendarEvent]:
        departure = timestamp(departure_time, "departure_time", error=InvalidInputError)
        return [event for event in _events(events)
                if timestamp(event.start, "event.start") <= departure
                < timestamp(event.end, "event.end")]

    def free_windows(self, events: list[CalendarEvent], window_start: str,
                     window_end: str) -> list[tuple[str, str]]:
        start = timestamp(window_start, "window_start", error=InvalidInputError)
        end = timestamp(window_end, "window_end", error=InvalidInputError)
        if end <= start:
            raise InvalidInputError("Window end must be after window start.", agent="calendar")
        cursor = start
        windows = []
        for event in _events(events):
            busy_start = max(start, timestamp(event.start, "event.start"))
            busy_end = min(end, timestamp(event.end, "event.end"))
            if busy_start >= busy_end:
                continue
            if busy_start > cursor:
                windows.append((cursor.isoformat(), busy_start.isoformat()))
            cursor = max(cursor, busy_end)
        if cursor < end:
            windows.append((cursor.isoformat(), end.isoformat()))
        return windows
