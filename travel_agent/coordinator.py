from dataclasses import asdict

from travel_agent.contracts import context_from_dict, validate_candidates
from travel_agent.models import TripContext
from travel_agent.planning.baseline import generate_baseline_candidates, summarize_recommendation

class TravelCoordinator:
    """Orchestrate travel capabilities without depending on a model provider."""

    def __init__(self, flight_tool, calendar_tool, planner):
        self.flight_tool = flight_tool
        self.calendar_tool = calendar_tool
        self.planner = planner

    def get_trip_context(self, flight_number: str, date: str) -> TripContext:
        flight = self.flight_tool.get_flight(flight_number, date)
        events = self.calendar_tool.get_events(date)

        context = TripContext(
            flight=flight,
            calendar_events=events,
            airport_travel_minutes=45,
            security_minutes=20,
            gate_walk_minutes=15,
            preferred_buffer_minutes=45,
        )

        return context_from_dict(asdict(context))

    def evaluate_trip_plans(self, context: TripContext, candidates=None) -> dict:
        context = context_from_dict(asdict(context))
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
