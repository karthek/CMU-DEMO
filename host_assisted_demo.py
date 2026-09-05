"""Simulate host tool calls locally, without a model SDK or MCP transport."""
from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool


def main():
    service = TravelService(TravelCoordinator(
        flight_tool=FakeFlightTool(),
        calendar_tool=FakeCalendarTool(),
        planner=BeamSearchPlanner(beam_width=2, depth=3),
    ))
    context = service.get_trip_context("DL1425", "2026-09-11")
    candidates = [
        {
            "label": label,
            "leave_time": f"2026-09-11T{time}",
            "summary": f"Host proposes leaving at {time}.",
            "history": ["Simulated host suggestion"],
        }
        for label, time in [
            ("Host early", "15:50"),
            ("Host buffer", "16:20"),
            ("Host meeting", "17:00"),
        ]
    ]
    print("Host fetched context for", context["flight"]["flight_number"])
    print("Host supplied:", ", ".join(plan["leave_time"] for plan in candidates))
    result = service.evaluate_trip_plans(context, candidates)
    print("Mode:", result["mode"])
    print(result["recommendation"])
    for plan in result["finalists"]:
        print(f"{plan['label']} | {plan['leave_time']} | score={plan['score']:.2f}")


if __name__ == "__main__":
    main()
