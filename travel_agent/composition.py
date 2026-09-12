"""Assemble the simulated application without putting data-source setup in services."""
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.planning.policy import DEFAULT_PLANNING_POLICY
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.transport_tool import FakeTransportTool


def create_simulated_coordinator(*, policy=DEFAULT_PLANNING_POLICY) -> TravelCoordinator:
    return TravelCoordinator(
        flight_agent=FlightAgent(FakeFlightTool()),
        calendar_agent=CalendarAgent(FakeCalendarTool()),
        transport_agent=TransportAgent(FakeTransportTool()),
        planner=BeamSearchPlanner(beam_width=2, depth=3, policy=policy),
    )


def create_itinerary_service(*, database_path=None, itinerary_path=None, flight_path=None, clock=None):
    """V8 assembly is separate from the frozen V7 demonstration composition."""
    from pathlib import Path
    from travel_agent.agents.itinerary_agent import ItineraryAgent
    from travel_agent.itinerary.clock import SystemClock
    from travel_agent.itinerary.repository import ItineraryRepository
    from travel_agent.itinerary.service import ItineraryService
    from travel_agent.itinerary.source import FixtureItinerarySource
    from travel_agent.service import TravelService
    from travel_agent.tools.fixture_flight_tool import FixtureFlightTool

    root = Path(__file__).resolve().parents[1]
    coordinator = TravelCoordinator(
        flight_agent=FlightAgent(FixtureFlightTool(flight_path or root / "fixtures/v8/flights.json")),
        calendar_agent=CalendarAgent(FakeCalendarTool()),
        transport_agent=TransportAgent(FakeTransportTool()),
        planner=BeamSearchPlanner(beam_width=2, depth=3),
    )
    return ItineraryService(
        ItineraryAgent(FixtureItinerarySource(itinerary_path or root / "fixtures/v8/itineraries.json")),
        ItineraryRepository(database_path or root / ".runtime/v8.sqlite3"),
        TravelService(coordinator), clock or SystemClock(),
    )
