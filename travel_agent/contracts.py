"""Validate JSON-compatible inputs before they reach the V1 planner."""
from datetime import datetime

from travel_agent.models import CalendarEvent, FlightState, TripContext


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _timestamp(value, field):
    _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO date and time") from None
    if "T" not in value or parsed.tzinfo is not None:
        raise ValueError(f"{field} must use a local ISO date and time without a timezone")
    return parsed


def context_from_dict(data: dict) -> TripContext:
    if not isinstance(data, dict):
        raise ValueError("context must be an object")
    try:
        values = dict(data)
        flight = FlightState(**values.pop("flight"))
        raw_events = values.pop("calendar_events", [])
        if not isinstance(raw_events, list):
            raise ValueError("calendar_events must be a list")
        events = [CalendarEvent(**event) for event in raw_events]
        context = TripContext(flight=flight, calendar_events=events, **values)
    except (KeyError, TypeError):
        raise ValueError("context contains missing, unknown, or malformed fields") from None
    for field in ("flight_number", "origin", "destination", "status", "gate"):
        _text(getattr(flight, field), f"flight.{field}")
    departure = _timestamp(flight.departure_time, "flight.departure_time")
    boarding = _timestamp(flight.boarding_time, "flight.boarding_time")
    if boarding > departure:
        raise ValueError("boarding_time must not be after departure_time")
    for field in (
        "airport_travel_minutes", "security_minutes", "gate_walk_minutes",
        "preferred_buffer_minutes",
    ):
        value = getattr(context, field)
        if type(value) is not int or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if type(flight.delay_minutes) is not int or flight.delay_minutes < 0:
        raise ValueError("flight.delay_minutes must be a non-negative integer")
    for event in events:
        _text(event.title, "event.title")
        start = _timestamp(event.start, "event.start")
        end = _timestamp(event.end, "event.end")
        if end <= start:
            raise ValueError("event.end must be after event.start")
        if event.priority not in ("normal", "high"):
            raise ValueError("event.priority must be normal or high")
    return context


def validate_candidates(candidates: list[dict]) -> list[dict]:
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must be a non-empty list; omit them for baseline mode")
    validated = []
    for index, plan in enumerate(candidates):
        if not isinstance(plan, dict):
            raise ValueError(f"candidate {index} must be an object")
        for field in ("label", "leave_time", "summary"):
            _text(plan.get(field), f"candidate {index}.{field}")
        _timestamp(plan["leave_time"], f"candidate {index}.leave_time")
        history = plan.get("history", [])
        if not isinstance(history, list) or any(not isinstance(item, str) for item in history):
            raise ValueError(f"candidate {index}.history must be a list of strings")
        # Copy inputs and discard caller-supplied scores or unrelated fields.
        validated.append({
            "label": plan["label"],
            "leave_time": plan["leave_time"],
            "summary": plan["summary"],
            "history": list(history),
        })
    return validated
