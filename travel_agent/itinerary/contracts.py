"""Strict source and selector validation; no inference or fallback."""
from datetime import date
import re
from urllib.parse import quote
from travel_agent.itinerary.clock import TIME_BASIS, parse_local
from travel_agent.itinerary.models import BookedItinerary, BookedSegment


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Required nonempty text")
    return value.strip()


def identity(*parts):
    return "/".join(quote(text(part), safe="") for part in parts)


def time_basis(payload):
    if not isinstance(payload, dict) or payload.get("time_basis") != TIME_BASIS:
        raise ValueError("Expected America/New_York time_basis")


def flight_number(value):
    value = text(value).upper()
    if not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,4}[A-Z]?", value):
        raise ValueError("Invalid flight number")
    return value


def airport(value):
    value = text(value).upper()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Invalid airport")
    return value


def normalize_record(raw, source_id):
    if not isinstance(raw, dict) or set(raw) != {"itinerary_id", "segments"}:
        raise ValueError("Invalid itinerary")
    source_itinerary_id = text(raw["itinerary_id"])
    itinerary_id = identity(source_id, source_itinerary_id)
    if not isinstance(raw["segments"], list) or not raw["segments"]:
        raise ValueError("Expected segments")
    segments, duplicates = {}, 0
    for item in raw["segments"]:
        if not isinstance(item, dict) or set(item) != {"segment_id", "flight_number", "departure_date", "origin",
                                                       "destination", "scheduled_departure", "booking_status"}:
            raise ValueError("Invalid segment")
        source_segment_id = text(item["segment_id"])
        departure = parse_local(item["scheduled_departure"])
        day = date.fromisoformat(item["departure_date"])
        if day.isoformat() != item["departure_date"] or day != departure.date():
            raise ValueError("Departure date mismatch")
        status = text(item["booking_status"]).upper()
        if status not in ("CONFIRMED", "CANCELLED"):
            raise ValueError("Invalid booking status")
        segment = BookedSegment(itinerary_id, identity(source_id, source_itinerary_id, source_segment_id),
                                source_segment_id, flight_number(item["flight_number"]), day.isoformat(),
                                airport(item["origin"]), airport(item["destination"]), departure, status)
        if source_segment_id in segments:
            if segments[source_segment_id] != segment:
                raise IdentityCollision("Conflicting segment identity")
            duplicates += 1
        segments[source_segment_id] = segment
    return BookedItinerary(itinerary_id, source_id, source_itinerary_id,
                          tuple(sorted(segments.values(), key=lambda s: s.segment_id))), duplicates


class IdentityCollision(ValueError):
    pass


def validate_selector(selector):
    if not isinstance(selector, dict) or set(selector) - {"itinerary_id", "segment_id", "destination", "departure_date"}:
        raise ValueError("Invalid selector")
    result = {key: text(value) for key, value in selector.items()}
    if "segment_id" in result and "itinerary_id" not in result:
        raise ValueError("segment_id requires itinerary_id")
    if "destination" in result:
        result["destination"] = airport(result["destination"])
    if "departure_date" in result and date.fromisoformat(result["departure_date"]).isoformat() != result["departure_date"]:
        raise ValueError("Invalid departure date")
    return result
