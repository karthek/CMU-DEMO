"""Bounded, deterministic template extraction. No network, guessing, or activation."""
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import re
from typing import Protocol
from zoneinfo import ZoneInfoNotFoundError

from travel_agent.live.mail import MailMessage
from travel_agent.live.time import source_instant


class ExtractionState(StrEnum):
    BOOKING = "BOOKING"
    CHANGE = "CHANGE"
    CANCELLATION = "CANCELLATION"
    UNRESOLVED = "UNRESOLVED"
    NOT_TRAVEL = "NOT_TRAVEL"


@dataclass(frozen=True)
class ExtractedSegment:
    carrier: str
    flight_number: str
    origin: str
    destination: str
    departure_date: date
    scheduled_departure: datetime | None
    origin_timezone: str | None
    scheduled_arrival: datetime | None
    destination_timezone: str | None
    booking_reference: str | None
    traveler_reference: str | None
    segment_reference: str | None


@dataclass(frozen=True)
class ExtractionResult:
    state: ExtractionState
    message: MailMessage
    rule_id: str | None
    segment: ExtractedSegment | None = None
    issues: tuple[str, ...] = ()

    @property
    def eligible_for_reconciliation(self):
        return self.segment is not None and self.state in (
            ExtractionState.BOOKING, ExtractionState.CHANGE, ExtractionState.CANCELLATION)


class ExtractionRule(Protocol):
    rule_id: str

    def matches(self, message: MailMessage) -> bool: ...
    def extract(self, message: MailMessage) -> ExtractionResult: ...


class NorthstarRule:
    """Synthetic single-segment template v1, not a claim of real airline support.

    Exact labeled fields must stand alone on lines. Repeated identical fields in
    multipart alternatives are accepted; contradictory values fail closed.
    Sender checking is a template constraint, not email authentication evidence.
    """
    rule_id = "synthetic-northstar/v1"
    carrier = "NS"
    family = "synthetic-northstar"
    event_types = frozenset((ExtractionState.BOOKING, ExtractionState.CHANGE, ExtractionState.CANCELLATION))
    subjects = {"Northstar Air booking confirmation": ExtractionState.BOOKING,
                "Northstar Air schedule change": ExtractionState.CHANGE,
                "Northstar Air flight cancellation": ExtractionState.CANCELLATION}
    labels = ("Event", "Carrier", "Flight", "Origin", "Destination", "Departure date",
              "Departure", "Origin timezone", "Arrival", "Destination timezone",
              "Booking reference", "Traveler reference", "Segment reference")

    def matches(self, message):
        return message.subject in self.subjects

    def extract(self, message):
        def unresolved(reason):
            return ExtractionResult(ExtractionState.UNRESOLVED, message, self.rule_id, issues=(reason,))

        if message.sender.rsplit("@", 1)[1] != "northstar.example.test":
            return unresolved("UNSUPPORTED_SENDER")
        if message.provider == "UNSPECIFIED" or message.provenance is None:
            return unresolved("MISSING_PROVENANCE")
        values = {label: set() for label in self.labels}
        for body in message.bodies:
            for line in body.splitlines():
                label, separator, value = line.partition(":")
                if separator and label in values:
                    values[label].add(value.strip())
        if any(len(v) > 1 for v in values.values()):
            return unresolved("CONFLICTING_FIELDS")
        fields = {k: next(iter(v)) for k, v in values.items() if v}
        required = ("Event", "Carrier", "Flight", "Origin", "Destination", "Departure date")
        if any(not fields.get(k) for k in required):
            return unresolved("MISSING_IDENTITY_FIELDS")
        state = self.subjects[message.subject]
        if fields["Event"] != state.value:
            return unresolved("CONFLICTING_EVENT")
        try:
            if fields["Carrier"] != "NS" or not re.fullmatch(r"NS[1-9][0-9]{0,3}", fields["Flight"]):
                raise ValueError("Invalid carrier/flight")
            # A deliberately bounded registry for this synthetic template.
            zones = {"ATL": "America/New_York", "PHL": "America/New_York", "LAX": "America/Los_Angeles"}
            origin, destination = fields["Origin"], fields["Destination"]
            if origin not in zones or destination not in zones or origin == destination:
                raise ValueError("Unsupported route")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["Departure date"]):
                raise ValueError("Explicit ISO departure date required")
            day = date.fromisoformat(fields["Departure date"])

            def timestamp(label, zone_label, airport):
                value, zone = fields.get(label), fields.get(zone_label)
                if value is None and zone is None:
                    return None
                if not value or zone != zones[airport] or "T" not in value:
                    raise ValueError("Explicit timestamp and supported airport zone required")
                return source_instant(value, zone_name=zone)

            departure = timestamp("Departure", "Origin timezone", origin)
            arrival = timestamp("Arrival", "Destination timezone", destination)
            if departure is not None and datetime.fromisoformat(fields["Departure"]).date() != day:
                raise ValueError("Date disagrees with departure")
            if arrival is not None and (departure is None or arrival <= departure):
                raise ValueError("Arrival requires departure and must follow it")
            for label in ("Booking reference", "Traveler reference", "Segment reference"):
                if label in fields and not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,63}", fields[label]):
                    raise ValueError("Invalid reference")
            segment = ExtractedSegment("NS", fields["Flight"], origin, destination, day,
                departure, fields.get("Origin timezone"), arrival, fields.get("Destination timezone"),
                fields.get("Booking reference"), fields.get("Traveler reference"), fields.get("Segment reference"))
        except (ValueError, ZoneInfoNotFoundError):
            return unresolved("INVALID_OR_CONFLICTING_FIELDS")
        return ExtractionResult(state, message, self.rule_id, segment)


class ItineraryExtractor:
    def __init__(self, rules: tuple[ExtractionRule, ...] | None = None, *, registry=None):
        from travel_agent.live.template_registry import TemplateRegistry
        if registry is not None and rules is not None:
            raise ValueError("Choose registry or legacy rules")
        self.registry = TemplateRegistry.from_rules((NorthstarRule(),) if rules is None else rules) if registry is None else registry
        if not isinstance(self.registry, TemplateRegistry):
            raise ValueError("Typed template registry required")
        self.rules = tuple(t.parser for t in self.registry.templates)

    def extract(self, message: MailMessage) -> ExtractionResult:
        return self.registry.extract(message)
