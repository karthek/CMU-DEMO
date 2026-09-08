from dataclasses import replace

from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.agents.errors import InvalidDataError
from travel_agent.contracts import validate_candidates
from travel_agent.models import TripContext
from travel_agent.planning.baseline import generate_baseline_candidates, summarize_recommendation

class TravelCoordinator:
    """Orchestrate travel capabilities without depending on a model provider."""

    def __init__(self, flight_tool=None, calendar_tool=None, planner=None, *,
                 flight_agent=None, calendar_agent=None, transport_agent=None):
        # Preserve the V1-V4 construction API; even legacy callers now use agents.
        if flight_tool is not None and flight_agent is not None:
            raise TypeError("Supply flight_agent or flight_tool, not both")
        if calendar_tool is not None and calendar_agent is not None:
            raise TypeError("Supply calendar_agent or calendar_tool, not both")
        if flight_agent is None:
            if flight_tool is None:
                raise TypeError("flight_agent is required")
            flight_agent = FlightAgent(flight_tool)
        if calendar_agent is None:
            if calendar_tool is None:
                raise TypeError("calendar_agent is required")
            calendar_agent = CalendarAgent(calendar_tool)
        if transport_agent is None:
            if flight_tool is None or calendar_tool is None:
                raise TypeError("transport_agent is required with agent-based construction")
            from travel_agent.tools.transport_tool import FakeTransportTool
            transport_agent = TransportAgent(FakeTransportTool())
        if planner is None:
            raise TypeError("planner is required")
        self.flight_agent = flight_agent
        self.calendar_agent = calendar_agent
        self.transport_agent = transport_agent
        self.planner = planner

    def get_trip_context(self, flight_number: str, date: str) -> TripContext:
        flight = self.flight_agent.get_flight(flight_number, date)
        events = self.calendar_agent.get_events(date)
        # Map the agent vocabulary to the existing planner vocabulary explicitly.
        priorities = {"normal": "normal", "high": "high"}
        mapped_events = []
        for event in events:
            if event.priority not in priorities:
                raise InvalidDataError(
                    "Calendar priority is unsupported by the planner; use normal or high.",
                    agent="calendar",
                )
            mapped_events.append(replace(event, priority=priorities[event.priority]))
        transport = self.transport_agent.get_estimate()

        context = TripContext(
            flight=flight,
            calendar_events=mapped_events,
            # Only driving/travel time maps into V1's airport_travel_minutes.
            # Parking and terminal walking remain outside this scoring model.
            airport_travel_minutes=transport.travel_minutes,
            security_minutes=20,
            gate_walk_minutes=15,
            preferred_buffer_minutes=45,
        )

        return context

    def evaluate_trip_plans(self, context: TripContext, candidates=None) -> dict:
        # Agent-built context is trusted. TravelService validates external context.
        mode = "deterministic" if candidates is None else "host-assisted"
        if candidates is None:
            candidates = generate_baseline_candidates(context)
        candidates = validate_candidates(candidates)
        finalists = self.planner.search(context, candidates)

        recommendation = summarize_recommendation(
            context=context,
            finalists=finalists,
        )

        return {
            "mode": mode,
            "selected_plan": finalists[0],
            "recommendation": recommendation,
            "finalists": finalists,
        }

    def plan_trip(self, flight_number: str, date: str, candidates=None) -> dict:
        context = self.get_trip_context(flight_number, date)
        return self.evaluate_trip_plans(context, candidates)
