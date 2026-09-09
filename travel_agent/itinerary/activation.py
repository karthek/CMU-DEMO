"""Pure automatic eligibility, independent of execution history and planning."""
from datetime import timedelta
from travel_agent.itinerary.clock import validate_local
from travel_agent.itinerary.contracts import airport, flight_number, text
from travel_agent.itinerary.models import ActivationResult, ActivationStatus as S, BookedSegment
from travel_agent.itinerary.policy import DEFAULT_ACTIVATION_POLICY


class ActivationEvaluator:
    def evaluate(self, segment, as_of, policy=DEFAULT_ACTIVATION_POLICY):
        validate_local(as_of)
        evidence = dict(rule="AUTOMATIC_PLANNING_WINDOW_V1", lower_bound_inclusive=True,
                        upper_bound_exclusive=True, at_or_after_activation=None,
                        before_departure=None, booking_status=None, validation_codes=[])
        departure = activation = minutes = None
        try:
            if not isinstance(segment, BookedSegment) or not segment.segment_id or not segment.itinerary_id:
                raise ValueError("Invalid segment")
            for value in (segment.itinerary_id, segment.segment_id, segment.source_segment_id):
                text(value)
            flight_number(segment.flight_number)
            airport(segment.origin)
            airport(segment.destination)
            departure = validate_local(segment.scheduled_departure)
            if departure.date().isoformat() != segment.departure_date:
                raise ValueError("Invalid departure date")
            if segment.booking_status not in ("CONFIRMED", "CANCELLED"):
                raise ValueError("Invalid booking status")
            activation = departure - timedelta(minutes=policy.planning_lead_time_minutes)
            minutes = (departure - as_of).total_seconds() / 60
            evidence.update(at_or_after_activation=as_of >= activation,
                            before_departure=as_of < departure, booking_status=segment.booking_status)
            if segment.booking_status == "CANCELLED":
                status, reason = S.CANCELLED, "BOOKING_CANCELLED"
            elif as_of >= departure:
                status, reason = S.DEPARTED, "DEPARTURE_REACHED"
            elif as_of < activation:
                status, reason = S.NOT_YET_ELIGIBLE, "BEFORE_PLANNING_WINDOW"
            else:
                status, reason = S.ELIGIBLE, "WITHIN_PLANNING_WINDOW"
        except (ValueError, TypeError, OverflowError):
            departure = activation = minutes = None
            status, reason = S.INVALID_SEGMENT, "SEGMENT_VALIDATION_FAILED"
            evidence["validation_codes"] = [reason]
        return ActivationResult(getattr(segment, "itinerary_id", None), getattr(segment, "segment_id", None),
                                status, status == S.ELIGIBLE, as_of, departure, activation,
                                policy.planning_lead_time_minutes, minutes, reason, evidence)
