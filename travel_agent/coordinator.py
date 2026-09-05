from travel_agent.models import TripContext

class TravelCoordinator:
    def __init__(self, provider, flight_tool, calendar_tool, planner):
        self.provider = provider
        self.flight_tool = flight_tool
        self.calendar_tool = calendar_tool
        self.planner = planner

    def plan_trip(self, flight_number: str, date: str):
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

        candidates = self.provider.generate_candidate_plans(context, count=4)
        finalists = self.planner.search(context, candidates)

        recommendation = self.provider.summarize_recommendation(
            context=context,
            finalists=finalists,
        )

        return {
            "recommendation": recommendation,
            "finalists": finalists,
        }
