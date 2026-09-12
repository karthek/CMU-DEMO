"""Explicit aware instants; no implicit airport or machine timezone."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware datetime required")
    instant = value.astimezone(timezone.utc)
    # Reject imaginary ZoneInfo datetimes created directly by replace(tzinfo=...).
    if isinstance(value.tzinfo, ZoneInfo):
        restored = instant.astimezone(value.tzinfo)
        if restored.replace(tzinfo=None) != value.replace(tzinfo=None) or restored.fold != value.fold:
            raise ValueError("Invalid local time/fold")
    return instant


def parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Aware ISO datetime required")
    return utc(datetime.fromisoformat(value))


def from_local(value: datetime, zone_name: str, *, fold: int | None = None) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("Naive local source time required")
    if not isinstance(zone_name, str) or not zone_name:
        raise ValueError("Explicit source timezone required")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise ValueError("fold must be 0 or 1")
    zone = ZoneInfo(zone_name)
    valid = {}
    for candidate_fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=candidate_fold)
        instant = aware.astimezone(timezone.utc)
        back = instant.astimezone(zone)
        if back.replace(tzinfo=None) == value and back.fold == candidate_fold:
            valid[candidate_fold] = instant
    if not valid:
        raise ValueError("Nonexistent source local time")
    if len(valid) > 1 and fold is None:
        raise ValueError("Ambiguous source local time requires explicit fold")
    chosen = next(iter(valid)) if fold is None else fold
    if chosen not in valid:
        raise ValueError("Invalid fold for source local time")
    return valid[chosen]


def legacy_v8_instant(value: str, *, time_basis: str) -> datetime:
    """Pure migration preparation; never rewrites historical V8 JSON."""
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Legacy local ISO time required")
    return from_local(datetime.fromisoformat(value), time_basis)


def source_instant(value: str, *, zone_name: str) -> datetime:
    """Check a source's numeric offset agrees with its declared airport/source zone."""
    parsed = datetime.fromisoformat(value)
    instant = utc(parsed)
    local = instant.astimezone(ZoneInfo(zone_name))
    if local.utcoffset() != parsed.utcoffset() or local.replace(tzinfo=None) != parsed.replace(tzinfo=None):
        raise ValueError("Source offset and declared timezone disagree")
    return instant
