from travel_agent.coordinator import TravelCoordinator
from travel_agent.providers.mock_provider import MockProvider
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.planning.beam_search import BeamSearchPlanner

def main():
    coordinator = TravelCoordinator(
        provider=MockProvider(),
        flight_tool=FakeFlightTool(),
        calendar_tool=FakeCalendarTool(),
        planner=BeamSearchPlanner(beam_width=2, depth=3),
    )

    result = coordinator.plan_trip(
        flight_number="DL1425",
        date="2026-09-11",
    )

    print("\n=== RECOMMENDED PLAN ===")
    print(result["recommendation"])

    print("\n=== FINALISTS ===")
    for i, plan in enumerate(result["finalists"], 1):
        print(f"{i}. {plan['label']} | score={plan['score']:.2f}")
        print(f"   {plan['summary']}")

if __name__ == "__main__":
    main()
