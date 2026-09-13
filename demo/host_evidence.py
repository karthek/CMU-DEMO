"""Untrusted host booking data -> existing TripContext, without booking ingestion.

The envelope is demo-only. Its provenance sidecar is not persisted by MCP or
treated as proof of authenticity. This bounded bridge supports the fixed demo's
route/date/time basis; unsupported evidence fails rather than using fixture facts.
"""
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum

from travel_agent.contracts import context_from_dict
from travel_agent.itinerary.clock import TIME_BASIS, parse_local
from travel_agent.itinerary.contracts import airport, flight_number, validate_selector
from demo.rehearsal_policy import AuthoritativeFailure


class EvidenceValidationError(AuthoritativeFailure):
    """Data failure, never permission for CP5 infrastructure fallback."""


class EvidenceSource(StrEnum):
    RECORDED = "RECORDED_HOST_EVIDENCE"
    LIVE = "LIVE_HOST_EVIDENCE"


class EvidenceProvider(StrEnum):
    SYNTHETIC = "SYNTHETIC_RECORDING"
    GMAIL_HOST = "GMAIL_VIA_HOST"


@dataclass(frozen=True, slots=True)
class HostBookingEvidence:
    source_type: EvidenceSource
    provider: EvidenceProvider
    flight_number: str
    departure_airport: str
    arrival_airport: str
    departure_date: str
    scheduled_departure: str
    time_basis: str
    scheduled_arrival: str | None = None
    evidence_timestamp: str | None = None

    def __post_init__(self):
        try:
            object.__setattr__(self, "source_type", EvidenceSource(self.source_type))
            object.__setattr__(self, "provider", EvidenceProvider(self.provider))
            if self.source_type == EvidenceSource.LIVE and self.provider == EvidenceProvider.SYNTHETIC:
                raise ValueError("Synthetic evidence cannot be labeled live")
            object.__setattr__(self, "flight_number", flight_number(self.flight_number))
            object.__setattr__(self, "departure_airport", airport(self.departure_airport))
            object.__setattr__(self, "arrival_airport", airport(self.arrival_airport))
            if self.time_basis != TIME_BASIS or self.departure_airport == self.arrival_airport:
                raise ValueError("Unsupported time basis or route")
            day = validate_selector({"departure_date": self.departure_date})["departure_date"]
            object.__setattr__(self, "departure_date", day)
            departure = parse_local(self.scheduled_departure)
            if day != departure.date().isoformat():
                raise ValueError("Departure date mismatch")
            if self.scheduled_arrival is not None and parse_local(self.scheduled_arrival) <= departure:
                raise ValueError("Arrival must follow departure")
            if self.evidence_timestamp is not None:
                parse_local(self.evidence_timestamp)
        except (ValueError, TypeError, AttributeError) as exc:
            raise EvidenceValidationError("Invalid structured booking evidence; no fixture substitution") from exc

    @property
    def source_label(self):
        return "RECORDED HOST EVIDENCE" if self.source_type == EvidenceSource.RECORDED else "LIVE HOST EVIDENCE"


def parse_evidence(payload):
    required = {"source_type", "provider", "flight_number", "departure_airport", "arrival_airport",
                "departure_date", "scheduled_departure", "time_basis"}
    optional = {"scheduled_arrival", "evidence_timestamp"}
    if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - required - optional:
        raise EvidenceValidationError("Missing or unsupported evidence fields; decisions and instructions are not accepted")
    if any(not isinstance(payload[k], str) for k in required):
        raise EvidenceValidationError("Required evidence fields must be structured strings")
    return HostBookingEvidence(**payload)


def validate_scenario_evidence(evidence, scenario):
    """Exact supported scenario constraint, not a real-world booking confirmation."""
    booking = scenario["itineraries"]["records"][0]["segments"][0]
    if (evidence.flight_number, evidence.departure_airport, evidence.arrival_airport,
        evidence.departure_date, parse_local(evidence.scheduled_departure)) != (
        booking["flight_number"], booking["origin"], booking["destination"], booking["departure_date"],
        parse_local(booking["scheduled_departure"])):
        raise EvidenceValidationError("Booking evidence is incompatible with the bounded CMU scenario")
    if evidence.evidence_timestamp and parse_local(evidence.evidence_timestamp) > parse_local(scenario["as_of"]):
        raise EvidenceValidationError("Evidence timestamp is after the fixed scenario clock")


def evidence_to_context(evidence, supplemental_context):
    """Explicit mapping with fixture compatibility checks; never mutate inputs.

Booking identity/schedule come from evidence. Boarding, gate, status, delay,
calendar and durations remain supplemental fixture assumptions, not email facts.
"""
    context = deepcopy(supplemental_context)
    try:
        context_from_dict(context)
        flight = context["flight"]
        if (flight["flight_number"], flight["origin"], flight["destination"],
            parse_local(flight["departure_time"])) != (
            evidence.flight_number, evidence.departure_airport, evidence.arrival_airport,
            parse_local(evidence.scheduled_departure)):
            raise ValueError("Evidence and supplemental operational context conflict")
        departure = parse_local(evidence.scheduled_departure)
        flight.update(flight_number=evidence.flight_number, origin=evidence.departure_airport,
                      destination=evidence.arrival_airport,
                      departure_time=departure.isoformat(timespec="minutes" if not departure.second and not departure.microsecond else "auto"))
        context_from_dict(context)  # Production validation also runs again on the MCP server.
    except (ValueError, KeyError, TypeError) as exc:
        raise EvidenceValidationError("Incompatible supplemental context; no fixture substitution") from exc
    provenance = {
        "flight.flight_number": evidence.source_label,
        "flight.origin": evidence.source_label,
        "flight.destination": evidence.source_label,
        "flight.departure_time": evidence.source_label,
        "flight.boarding_time": "FIXTURE", "flight.status": "FIXTURE", "flight.gate": "FIXTURE",
        "flight.delay_minutes": "FIXTURE", "calendar_events": "FIXTURE",
        "airport_travel_minutes": "FIXTURE", "security_minutes": "FIXTURE",
        "gate_walk_minutes": "FIXTURE", "preferred_buffer_minutes": "FIXTURE",
    }
    return context, provenance
