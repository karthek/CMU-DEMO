"""Offline booking truth: exact identity, explicit document order, pure projection.

Template event permission is checked before event construction. Document order
still requires explicit lifetime evidence; only an explicit assertion linked to
the cancelled state can reinstate travel. Mail metadata has no vote.
"""
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from travel_agent.live.extraction import ExtractedSegment, ExtractionResult, ExtractionState
from travel_agent.live.time import utc


def stable_id(value):
    return sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


class Order(StrEnum):
    OLDER = "OLDER"
    NEWER = "NEWER"
    EQUAL = "EQUAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EventAuthority:
    lifetime: str
    sequence: int

    def __post_init__(self):
        if not isinstance(self.lifetime, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,63}", self.lifetime):
            raise ValueError("Explicit booking lifetime required")
        if type(self.sequence) is not int or not 0 <= self.sequence <= 2147483647:
            raise ValueError("Bounded airline sequence required")

    def compare(self, other):
        if not isinstance(other, EventAuthority) or self.lifetime != other.lifetime:
            return Order.UNKNOWN
        return Order.NEWER if self.sequence > other.sequence else Order.OLDER if self.sequence < other.sequence else Order.EQUAL


@dataclass(frozen=True)
class Reinstatement:
    cancelled_authority: EventAuthority
    segment_reference: str

    def __post_init__(self):
        if not isinstance(self.cancelled_authority, EventAuthority):
            raise ValueError("Typed cancellation authority required")
        if not isinstance(self.segment_reference, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,63}", self.segment_reference):
            raise ValueError("Explicit cancelled segment reference required")


@dataclass(frozen=True)
class BookingEvent:
    kind: ExtractionState
    segment: ExtractedSegment
    authority: EventAuthority | None
    issues: tuple[str, ...] = ()
    parser_version: str = "booking-events/v1"
    reinstatement: Reinstatement | None = None


def booking_event(result: ExtractionResult, *, policy=None) -> BookingEvent | None:
    """Construct only explicitly authorized exact-template events."""
    from travel_agent.live.template_authority import DEFAULT_TEMPLATE_EVENT_POLICY
    return (DEFAULT_TEMPLATE_EVENT_POLICY if policy is None else policy).construct(result)


class LifetimeStatus(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    UNKNOWN = "UNKNOWN"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class BookingLifetimeCompatibility:
    status: LifetimeStatus
    lifetimes: tuple[str, ...]
    has_unattributed_evidence: bool


def booking_lifetime_compatibility(events) -> BookingLifetimeCompatibility:
    """Compare explicit lifetimes across ALL coupons of one exact booking key.

    Unknown evidence is never assigned to a known lifetime. A shared PNR does not
    establish a relationship between two different explicit lifetimes.
    """
    events = tuple(events)
    lifetimes = tuple(sorted({e.authority.lifetime for e in events if e.authority is not None}))
    unknown = not events or any(e.authority is None for e in events)
    status = LifetimeStatus.UNRESOLVED if len(lifetimes) > 1 else LifetimeStatus.UNKNOWN if unknown else LifetimeStatus.COMPATIBLE
    return BookingLifetimeCompatibility(status, lifetimes, unknown)


def constrain_booking_lifetime(segment, compatibility):
    """A booking collision cannot publish active segments or undo cancellation."""
    if compatibility.status != "UNRESOLVED" or segment.status == "CANCELLED":
        return segment
    return CanonicalSegment(segment.segment_id, segment.booking_id, "UNRESOLVED", None,
        tuple(sorted(set((*segment.reasons, "BOOKING_LIFETIME_COLLISION")))))


@dataclass(frozen=True)
class CanonicalBooking:
    booking_id: str
    carrier: str
    booking_reference: str
    traveler_reference: str
    lifetime_compatibility: BookingLifetimeCompatibility | None = None


@dataclass(frozen=True)
class CanonicalSegment:
    segment_id: str
    booking_id: str
    status: str
    schedule: ExtractedSegment | None
    reasons: tuple[str, ...]
    current_authority: EventAuthority | None = None


@dataclass(frozen=True)
class PlanningReadySegment:
    """Booking-only handoff; does not grant operational/live planning authority."""
    segment_id: str
    booking_id: str
    schedule: ExtractedSegment


@dataclass(frozen=True)
class PlanningReadiness:
    ready: PlanningReadySegment | None
    reasons: tuple[str, ...]

    @property
    def planning_allowed(self):
        return self.ready is not None


def planning_readiness(segment, *, authorized_travelers):
    reasons = list(segment.reasons)
    if segment.status != "BOOKED":
        reasons.append(segment.status)
    schedule = segment.schedule
    if schedule is None:
        reasons.append("NO_CURRENT_SCHEDULE")
    else:
        if schedule.traveler_reference not in authorized_travelers:
            reasons.append("TRAVELER_NOT_AUTHORIZED")
        if schedule.scheduled_departure is None:
            reasons.append("MISSING_DEPARTURE")
        if not schedule.origin_timezone:
            reasons.append("MISSING_ORIGIN_TIMEZONE")
        try:
            zone = ZoneInfo(schedule.origin_timezone) if schedule.origin_timezone else None
            if schedule.scheduled_departure is not None:
                instant = utc(schedule.scheduled_departure)
                if zone and instant.astimezone(zone).date() != schedule.departure_date:
                    reasons.append("DEPARTURE_DATE_CONFLICT")
            if schedule.scheduled_arrival is not None:
                arrival = utc(schedule.scheduled_arrival)
                if schedule.scheduled_departure is None or arrival <= utc(schedule.scheduled_departure):
                    reasons.append("INVALID_ARRIVAL_TIME")
                if not schedule.destination_timezone:
                    reasons.append("MISSING_DESTINATION_TIMEZONE")
                else:
                    ZoneInfo(schedule.destination_timezone)
        except (ValueError, KeyError):
            reasons.append("INVALID_DEPARTURE_TIME")
        if not all(isinstance(a, str) and re.fullmatch(r"[A-Z]{3}", a) for a in (schedule.origin, schedule.destination)) or schedule.origin == schedule.destination:
            reasons.append("INVALID_ROUTE")
        if not schedule.flight_number:
            reasons.append("MISSING_FLIGHT_NUMBER")
    reasons = tuple(sorted(set(reasons)))
    return PlanningReadiness(None if reasons else PlanningReadySegment(segment.segment_id, segment.booking_id, schedule), reasons)


def project(segment_id, booking_id, events):
    """All-history projection, independent of arrival order. No last-write-wins."""
    events = tuple(events)
    if not events:
        raise ValueError("Projection requires evidence")
    facts = {(e.kind, e.segment, e.reinstatement) for e in events}
    conflict = any(e.issues for e in events)
    authorities = [e.authority for e in events]
    # Different lifetimes never supersede each other under a reused reference.
    if len({a.lifetime for a in authorities if a}) > 1:
        conflict = True
    if len(facts) == 1:
        selected = max(events, key=lambda e: e.authority.sequence if e.authority else -1)
    elif any(a is None for a in authorities):
        conflict, selected = True, None
    else:
        # Contradictory claims at ANY identical sequence poison the history.
        by_order = {}
        for event in events:
            by_order.setdefault(event.authority.sequence, set()).add((event.kind, event.segment, event.reinstatement))
        conflict |= any(len(values) > 1 for values in by_order.values())
        highest = max(a.sequence for a in authorities)
        selected = next(e for e in events if e.authority.sequence == highest)
    if conflict:
        return CanonicalSegment(segment_id, booking_id, "UNRESOLVED", None, ("CONFLICTING_EVENT_HISTORY",))
    if any(e.reinstatement is not None for e in events) or any(e.kind == ExtractionState.CANCELLATION for e in events):
        def unresolved():
            return CanonicalSegment(segment_id, booking_id, "UNRESOLVED", None, ("INVALID_REINSTATEMENT_HISTORY",))

        if all(authorities):
            # Validate explicit links against immutable cancellation evidence in
            # this canonical segment. Never use arrival order or a prior DB value.
            cancellations = {e.authority for e in events if e.kind == ExtractionState.CANCELLATION}
            for event in events:
                assertion = event.reinstatement
                if assertion is not None and (
                    assertion.cancelled_authority not in cancellations
                    or assertion.segment_reference != event.segment.segment_reference
                    or event.authority.compare(assertion.cancelled_authority) != Order.NEWER
                    or event.kind == ExtractionState.CANCELLATION
                ):
                    return unresolved()
            selected = None
            # Equal-sequence contradictions were rejected above; equivalent
            # provider copies represent just one transition.
            ordered = {e.authority.sequence: e for e in events}
            for _, event in sorted(ordered.items()):
                if event.kind == ExtractionState.CANCELLATION:
                    selected = event
                elif event.reinstatement is not None:
                    if (selected is None or selected.kind != ExtractionState.CANCELLATION
                            or event.reinstatement.cancelled_authority != selected.authority):
                        return unresolved()
                    selected = event
                elif selected is None or selected.kind != ExtractionState.CANCELLATION:
                    selected = event
                # A normal BOOKING/CHANGE cannot cross the cancellation barrier.
        elif any(e.reinstatement is not None for e in events):
            return unresolved()
    status = "CANCELLED" if selected.kind == ExtractionState.CANCELLATION else "BOOKED"
    authority = selected.authority if all(authorities) else None
    return CanonicalSegment(segment_id, booking_id, status, selected.segment, (), authority)
