"""Transport-neutral operations for a future MCP or other host adapter."""
from dataclasses import asdict
from datetime import date as calendar_date, datetime

from travel_agent.contracts import context_from_dict
from travel_agent.coordinator import TravelCoordinator


def _context_payload(context) -> dict:
    """Keep V2 wire formatting while agents retain normalized timestamps internally."""
    payload = asdict(context)
    for item, fields in [(payload["flight"], ("departure_time", "boarding_time"))] + [
        (event, ("start", "end")) for event in payload["calendar_events"]
    ]:
        for field in fields:
            value = datetime.fromisoformat(item[field])
            precision = "minutes" if value.second == 0 and value.microsecond == 0 else "auto"
            item[field] = value.isoformat(timespec=precision)
    return payload


class TravelService:
    def __init__(self, coordinator: TravelCoordinator):
        self.coordinator = coordinator

    def get_trip_context(self, flight_number: str, date: str) -> dict:
        if not isinstance(flight_number, str) or not flight_number.strip():
            raise ValueError("flight_number must be a non-empty string")
        try:
            parsed = calendar_date.fromisoformat(date)
            if parsed.isoformat() != date:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("date must use YYYY-MM-DD") from None
        return _context_payload(self.coordinator.get_trip_context(flight_number, date))

    def evaluate_trip_plans(self, context: dict, candidates=None) -> dict:
        return self.coordinator.evaluate_trip_plans(context_from_dict(context), candidates)
