"""Small validation helpers shared only by the independent V4 agents."""
from datetime import date, datetime
from travel_agent.agents.errors import InvalidDataError, InvalidInputError


def text(value, field, *, agent, error=InvalidDataError):
    if not isinstance(value, str) or not value.strip():
        raise error(f"{field} must be a non-empty string.", agent=agent)
    return value.strip()


def travel_date(value, *, agent):
    if not isinstance(value, str):
        raise InvalidInputError("Travel date must use YYYY-MM-DD.", agent=agent)
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
    except ValueError as exc:
        raise InvalidInputError("Travel date must use YYYY-MM-DD.", agent=agent) from exc
    return parsed


def timestamp(value, field, *, agent, error=InvalidDataError):
    value = text(value, field, agent=agent, error=error)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise error(f"{field} must be a local ISO date/time.", agent=agent) from exc
    if "T" not in value or parsed.tzinfo is not None:
        raise error(f"{field} must be a local ISO date/time without timezone.", agent=agent)
    return parsed


def duration(value, field, *, agent):
    if type(value) is not int or value < 0:
        raise InvalidDataError(f"{field} must be a non-negative integer.", agent=agent)
    return value
