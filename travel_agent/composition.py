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
