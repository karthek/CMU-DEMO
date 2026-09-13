"""Typed demo-only transport and benefit evidence. No provider retrieval or scoring."""
from dataclasses import dataclass, fields
import json
import math
from pathlib import Path

from demo.host_evidence import EvidenceValidationError
from travel_agent.itinerary.clock import parse_local


def read_json(path):
    def unique(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate fields")
        return result
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
        if not isinstance(payload, dict):
            raise ValueError("Evidence must be an object")
        return payload
    except (ValueError, OSError) as exc:
        raise EvidenceValidationError("Invalid evidence JSON; no fixture substitution") from exc


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid evidence text")


@dataclass(frozen=True, slots=True)
class TransportEvidence:
    origin_type: str
    origin: str
    destination: str
    travel_mode: str
    distance_miles: float
    estimated_travel_minutes: int
    travel_date: str
    time_basis: str
    source_type: str
    source_reference: str
    retrieved_on: str

    def validate(self, scenario):
        for name in ("origin", "source_reference", "retrieved_on"):
            text(getattr(self, name))
        segment = scenario["itineraries"]["records"][0]["segments"][0]
        if (self.origin_type != "USER_SUPPLIED_LOCATION" or self.origin != scenario["user_origin"]
            or self.destination != segment["origin"] or self.travel_mode != "DRIVE"
            or self.travel_date != segment["departure_date"] or self.time_basis != "America/New_York"
            or self.source_type not in {"HOST_TRAVEL_EVIDENCE", "RECORDED_HOST_TRAVEL_EVIDENCE"}):
            raise ValueError("Transport does not match supplied origin and trip")
        if type(self.estimated_travel_minutes) is not int or not 0 < self.estimated_travel_minutes <= 1440:
            raise ValueError("Travel duration must be positive integer minutes")
        if type(self.distance_miles) not in (int, float) or not math.isfinite(self.distance_miles) or not 0 < self.distance_miles <= 2000:
            raise ValueError("Distance must be positive finite miles")
        _retrieval_date(self.retrieved_on, scenario)


@dataclass(frozen=True, slots=True)
class TravelBenefitEvidence:
    benefit_type: str
    airport: str
    eligible: bool
    usable_from: str
    usable_until: str
    time_basis: str
    facility_name: str
    facility_location: str
    amenities: tuple[str, ...]
    source_type: str
    source_reference: str
    provenance_label: str
    retrieved_on: str

    def validate(self, scenario):
        for name in ("facility_name", "facility_location", "source_reference", "provenance_label", "retrieved_on"):
            text(getattr(self, name))
        segment = scenario["itineraries"]["records"][0]["segments"][0]
        start, end = parse_local(self.usable_from), parse_local(self.usable_until)
        if (self.benefit_type != "AIRPORT_LOUNGE" or self.airport != segment["origin"]
            or type(self.eligible) is not bool or self.time_basis != "America/New_York"
            or self.source_type not in {"HOST_TRAVEL_BENEFIT_EVIDENCE", "RECORDED_HOST_TRAVEL_BENEFIT_EVIDENCE"}
            or not start < end <= parse_local(segment["scheduled_departure"])
            or start.date().isoformat() != segment["departure_date"]
            or end.date().isoformat() != segment["departure_date"]):
            raise ValueError("Invalid or incompatible benefit window")
        if (not isinstance(self.amenities, tuple) or len(set(self.amenities)) != len(self.amenities)
            or not set(self.amenities) <= {"FOOD", "WORKSPACE", "RELAXATION"}):
            raise ValueError("Unsupported amenities")
        _retrieval_date(self.retrieved_on, scenario)


def _retrieval_date(value, scenario):
    from datetime import date
    day = date.fromisoformat(value)
    if day.isoformat() != value or day > parse_local(scenario["as_of"]).date():
        raise ValueError("Invalid retrieval date")


def parse_domain(kind, payload, scenario):
    try:
        if not isinstance(payload, dict) or set(payload) != {f.name for f in fields(kind)}:
            raise ValueError("Missing or unknown evidence fields")
        values = dict(payload)
        if kind is TravelBenefitEvidence:
            if not isinstance(values["amenities"], list) or any(not isinstance(a, str) for a in values["amenities"]):
                raise ValueError("Amenities must be a string list")
            values["amenities"] = tuple(values["amenities"])
        evidence = kind(**values)
        evidence.validate(scenario)
        return evidence
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise EvidenceValidationError("Invalid structured domain evidence; no fixture substitution") from exc
