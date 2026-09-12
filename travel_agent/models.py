from dataclasses import dataclass, field
from typing import List

@dataclass(frozen=True)
class TransportEstimate:
    travel_minutes: int
    parking_minutes: int
    terminal_walk_minutes: int
    mode: str
    source: str

@dataclass
class FlightState:
    flight_number: str
    origin: str
    destination: str
    departure_time: str
    boarding_time: str
    status: str
    gate: str
    delay_minutes: int = 0

@dataclass
class CalendarEvent:
    title: str
    start: str
    end: str
    priority: str = "normal"

@dataclass
class TripContext:
    flight: FlightState
    calendar_events: List[CalendarEvent] = field(default_factory=list)
    airport_travel_minutes: int = 45
    security_minutes: int = 20
    gate_walk_minutes: int = 15
    preferred_buffer_minutes: int = 45
