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
from copy import deepcopy
from demo.search_trace import TracedBeamSearchPlanner, stage_rows, render_trace
from travel_agent.contracts import context_from_dict


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


def test_trace_parity_and_actual_stages():
    traced, plain = run_demo(), run_demo(traced=False)
    assert traced["booked_result"] == plain["booked_result"]
    result = traced["booked_result"]["run"]["planning_result"]
    assert [p["leave_time"][-5:] for p in result["finalists"]] == ["16:20", "16:10"]
    assert [p["score"] for p in result["finalists"]] == [76.8, 76.0]
    assert [s["stage"] for s in traced["trace"]] == [1, 2, 3]
    assert [len(s["entered"]) for s in traced["trace"]] == [3, 6, 6]
    assert traced["trace"][0]["entered"] == [dict(p, history=[]) for p in load_scenario()["candidates"]]
    for previous, stage in zip(traced["trace"], traced["trace"][1:]):
        assert stage["entered"][:2] == previous["retained"]
        rows = stage_rows(stage, previous["retained"])
        assert sum(r["parent"] is not None for r in rows) == 4
        assert stage["retained"] == stage["ranked"][:2]
    output = render_demo(traced)
    assert "Width: 2\nDepth: 3" in output
    assert "Stage 3 - Refinement 2" in output
    assert "duplicate/deduplicated" in output


def test_observer_detached_cannot_change_rank_output_or_inputs():
    report = run_demo(traced=False)
    context = context_from_dict(report["booked_result"]["run"]["context"])
    candidates = load_scenario()["candidates"]
    original = deepcopy(candidates)
    planner = TracedBeamSearchPlanner()
    expected = BeamSearchPlanner().search_outcome(context, candidates)

    def destructive_observer(entered, ranked, width):
        entered.clear()
        ranked[0]["score"] = -999
        ranked[0]["feasibility"]["feasible"] = False
        ranked.clear()
        return []

    with patch.object(planner, "_record", side_effect=destructive_observer):
        assert planner.search_outcome(context, candidates) == expected
    assert candidates == original
    first = planner.search_outcome(context, candidates)
    snapshots = deepcopy(planner.stages)
    assert planner.search_outcome(context, candidates) == first
    assert planner.stages == snapshots
    assert candidates == original


def test_infeasible_search_beam_and_duplicate_semantics():
    report = run_demo(traced=False)
    context = context_from_dict(report["booked_result"]["run"]["context"])
    candidates = [{"label": "Late", "leave_time": "2026-09-16T18:00", "summary": "Too late"},
                  {"label": "Duplicate", "leave_time": "2026-09-16T18:00:00", "summary": "Same instant"}]
    planner = TracedBeamSearchPlanner()
    result = planner.search_outcome(context, candidates)
    assert result == BeamSearchPlanner().search_outcome(context, candidates)
    rows = stage_rows(planner.stages[0])
    assert rows[0]["status"] == "retained" and not rows[0]["feasible"]
    assert rows[0]["score"] is None
    assert rows[1]["status"] == "duplicate/deduplicated"
    assert result.finalists == []
    assert "INFEASIBLE; search only" in render_trace(planner.stages, 2, 3, result.finalists)


def test_rank_return_is_exact_production_object():
    planner = TracedBeamSearchPlanner()
    ranked = [{"marker": []}]
    with patch.object(BeamSearchPlanner, "_rank", return_value=ranked):
        assert planner._rank(None, []) is ranked
    planner.stages[0]["ranked"][0]["marker"].append("observer change")
    assert ranked == [{"marker": []}]
