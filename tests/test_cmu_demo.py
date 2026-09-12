"""Checkpoint 1 exercises production planning with isolated offline evidence."""
import inspect
from pathlib import Path
from unittest.mock import patch

from demo.cmu_demo import load_scenario, render_demo, run_demo
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock, SystemClock
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService


def test_scenario_contains_proposals_and_evidence_only():
    scenario = load_scenario()
    assert scenario["itineraries"]["records"][0]["segments"][0]["origin"] == "ATL"
    assert scenario["as_of"] == "2026-09-16T12:00:00"
    forbidden = {"score", "score_breakdown", "feasibility", "selected_plan", "finalists"}

    def check(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(scenario)


def test_offline_fixed_clock_repeatability_and_production_handoff():
    paths = []

    def compose(**kwargs):
        assert isinstance(kwargs["clock"], FixedClock)
        assert kwargs["clock"].now().isoformat() == load_scenario()["as_of"]
        paths.append(Path(kwargs["database_path"]))
        service = create_itinerary_service(**kwargs)
        assert type(service.travel_service) is TravelService
        assert type(service.travel_service.coordinator.planner) is BeamSearchPlanner
        return service

    original = TravelService.evaluate_trip_plans
    calls = []

    def evaluate(self, context, candidates=None):
        result = original(self, context, candidates)
        calls.append(result)
        return result

    with patch.dict("os.environ", {}, clear=True), \
         patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")), \
         patch("socket.create_connection", side_effect=AssertionError("Network forbidden")), \
         patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")), \
         patch.object(SystemClock, "now", side_effect=AssertionError("Wall clock forbidden")), \
         patch("demo.cmu_demo.create_itinerary_service", side_effect=compose), \
         patch.object(TravelService, "evaluate_trip_plans", evaluate):
        first, second = run_demo(), run_demo()
    assert first == second
    assert render_demo(first) == render_demo(second)
    assert len(calls) == 2
    assert first["booked_result"]["run"]["planning_result"] == calls[0]
    assert calls[0]["status"] == "PLAN_FOUND"
    assert len(calls[0]["finalists"]) == 2
    assert calls[0]["selected_plan"]["score_breakdown"]
    assert calls[0]["selected_plan"]["feasibility"]["feasible"]
    assert paths[0] != paths[1]
    assert all(not path.parent.exists() for path in paths)


def test_no_calendar_write_surface_and_truthful_presentation():
    assert {name for name, _ in inspect.getmembers(CalendarAgent, inspect.isfunction)
            if not name.startswith("_")} == {
                "get_events", "last_meeting_end", "departure_conflicts", "free_windows"}
    report = run_demo()
    output = render_demo(report)
    assert "FIXTURE" in output
    assert "Best within explored candidates" in output
    assert "Calendar conflicts are soft scoring penalties" in output
    assert "No calendar mutation or approval/execution workflow" in output
    context = report["booked_result"]["run"]["context"]
    assert (context["airport_travel_minutes"], context["security_minutes"], context["gate_walk_minutes"]) == (45, 20, 15)
