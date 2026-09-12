from functools import partial
import re
from travel_agent.agents import _validation
from travel_agent.agents.errors import DataUnavailableError, InvalidDataError, InvalidInputError, ToolFailureError
from travel_agent.models import FlightState

text = partial(_validation.text, agent="flight")
timestamp = partial(_validation.timestamp, agent="flight")
duration = partial(_validation.duration, agent="flight")


class FlightAgent:
    """Retrieve and validate one flight; never score or rank travel plans."""

    def __init__(self, flight_tool):
        self._flight_tool = flight_tool

    def get_flight(self, flight_number: str, date: str) -> FlightState:
        flight_number = text(flight_number, "flight_number", error=InvalidInputError).upper()
        if not re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,4}[A-Z]?", flight_number):
            raise InvalidInputError("Flight number must contain a carrier code and flight digits.", agent="flight")
        day = _validation.travel_date(date, agent="flight")
        try:
            flight = self._flight_tool.get_flight(flight_number, date)
        except Exception as exc:
            raise ToolFailureError("Flight retrieval failed.", agent="flight") from exc
        if flight is None:
            raise DataUnavailableError("Flight information is unavailable.", agent="flight")
        if not isinstance(flight, FlightState):
            raise InvalidDataError("Flight data has an invalid structure.", agent="flight")
        fields = {field: text(getattr(flight, field), field) for field in (
            "flight_number", "origin", "destination", "status", "gate",
        )}
        fields["flight_number"] = fields["flight_number"].upper()
        if fields["flight_number"] != flight_number:
            raise InvalidDataError("Returned flight number does not match request.", agent="flight")
        departure = timestamp(flight.departure_time, "departure_time")
        boarding = timestamp(flight.boarding_time, "boarding_time")
        if departure.date() != day:
            raise InvalidDataError("Returned departure date does not match request.", agent="flight")
        if boarding > departure:
            raise InvalidDataError("Boarding time cannot be after departure time.", agent="flight")
        return FlightState(
            **fields,
            departure_time=departure.isoformat(),
            boarding_time=boarding.isoformat(),
            delay_minutes=duration(flight.delay_minutes, "delay_minutes"),
        )
