"""One documented local-time convention at the V8 boundary."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIME_BASIS = "America/New_York"


def local_zone():
    try:
        return ZoneInfo(TIME_BASIS)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("America/New_York timezone data unavailable; install tzdata.") from exc


def validate_local(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("V8 requires local date-times without timezone offsets")
    zone = local_zone()
    possibilities = set()
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            possibilities.add(instant)
    if len(possibilities) != 1:
        raise ValueError("Ambiguous or nonexistent America/New_York local time")
    return value.replace(fold=0)


def parse_local(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Expected local ISO date-time")
    return validate_local(datetime.fromisoformat(value))


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return validate_local(datetime.now(local_zone()).replace(tzinfo=None))


@dataclass(frozen=True)
class FixedClock:
    value: datetime

    def __post_init__(self):
        validate_local(self.value)

    def now(self) -> datetime:
        return self.value
