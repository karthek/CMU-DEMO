"""Strict V8 operational facts. Missing keys never synthesize a flight."""
import json
from pathlib import Path
from dataclasses import replace
from datetime import date
from travel_agent.models import FlightState
from travel_agent.itinerary.clock import parse_local
from travel_agent.itinerary.contracts import airport, flight_number, text, time_basis


class FixtureFlightTool:
    def __init__(self, path):
        self.path = Path(path)

    def get_flight(self, requested_number, requested_date):
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        time_basis(payload)
        if set(payload) != {"time_basis", "flights"} or not isinstance(payload.get("flights"), list):
            raise ValueError("Invalid operational flight snapshot")
        flights = {}
        for raw in payload["flights"]:
            if not isinstance(raw, dict):
                raise ValueError("Invalid operational flight")
            flight = FlightState(**raw)
            departure, boarding = parse_local(flight.departure_time), parse_local(flight.boarding_time)
            if boarding > departure or type(flight.delay_minutes) is not int or flight.delay_minutes < 0:
                raise ValueError("Invalid operational timing")
            flight = replace(flight, flight_number=flight_number(flight.flight_number), origin=airport(flight.origin),
                             destination=airport(flight.destination), gate=text(flight.gate), status=text(flight.status),
                             departure_time=departure.isoformat(), boarding_time=boarding.isoformat())
            key = (flight.flight_number, departure.date().isoformat())
            if key in flights and flights[key] != flight:
                raise ValueError("Conflicting operational flight fixtures")
            flights[key] = flight
        return flights.get((flight_number(requested_number), date.fromisoformat(requested_date).isoformat()))
