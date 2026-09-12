"""Versioned document interpreters. Policy registration separately grants authority."""
import re

from travel_agent.live.booking import BookingEvent, EventAuthority, Reinstatement, Order
from travel_agent.live.extraction import ExtractionState


def northstar_event_v1(result):
    """Preserve the frozen synthetic document interpretation and event bytes."""
    authority, issues, reinstatement = None, (), None
    order_labels = ("Booking lifetime", "Airline sequence")
    transition_labels = ("Travel state", "Reinstates lifetime",
                         "Reinstates cancellation sequence", "Reinstates segment reference")
    fields = {label: set() for label in (*order_labels, *transition_labels)}
    for body in result.message.bodies:
        for line in body.splitlines():
            label, separator, value = line.partition(":")
            if separator and label in fields:
                fields[label].add(value.strip())
    if any(fields[label] for label in order_labels):
        try:
            if any(len(fields[label]) != 1 for label in order_labels):
                raise ValueError("Incomplete or conflicting authority")
            lifetime = next(iter(fields["Booking lifetime"]))
            sequence = next(iter(fields["Airline sequence"]))
            if not re.fullmatch(r"0|[1-9][0-9]{0,9}", sequence):
                raise ValueError("Invalid sequence")
            authority = EventAuthority(lifetime, int(sequence))
        except ValueError:
            issues = ("INVALID_EVENT_AUTHORITY",)
    if any(fields[label] for label in transition_labels):
        try:
            if any(len(fields[label]) != 1 for label in transition_labels):
                raise ValueError("Incomplete or conflicting reinstatement")
            values = {label: next(iter(fields[label])) for label in transition_labels}
            sequence = values["Reinstates cancellation sequence"]
            if values["Travel state"] != "REINSTATED" or not re.fullmatch(r"0|[1-9][0-9]{0,9}", sequence):
                raise ValueError("Explicit reinstatement required")
            reinstatement = Reinstatement(EventAuthority(values["Reinstates lifetime"], int(sequence)),
                values["Reinstates segment reference"])
            if (authority is None or authority.compare(reinstatement.cancelled_authority) != Order.NEWER
                    or reinstatement.segment_reference != result.segment.segment_reference
                    or result.state == ExtractionState.CANCELLATION):
                raise ValueError("Invalid reinstatement linkage")
        except ValueError:
            reinstatement = None
            issues += ("INVALID_REINSTATEMENT",)
        return BookingEvent(result.state, result.segment, authority, issues, "booking-events/v2", reinstatement)
    return BookingEvent(result.state, result.segment, authority, issues)
