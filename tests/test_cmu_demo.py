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
from dataclasses import FrozenInstanceError, replace
import pytest
from demo.hitl import (CalendarChangeProposal, propose_calendar_changes,
                       CALENDAR_MUTATIONS_REQUIRE_EXPLICIT_APPROVAL)


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


def test_cp3_proposal_uses_authoritative_conflict_without_mutation():
    report = run_demo()
    run = report["booked_result"]["run"]
    context, result = run["context"], run["planning_result"]
    scenario = report["scenario"]
    before = deepcopy((context, result, scenario))
    proposals = propose_calendar_changes(context, result, scenario["calendar_proposal_slots"],
                                         as_of=scenario["as_of"])
    assert (context, result, scenario) == before
    assert proposals == report["calendar_proposals"]
    assert len(proposals) == 1
    proposal = proposals[0]
    conflict = result["selected_plan"]["calendar_conflicts"][0]
    event = context["calendar_events"][conflict["event_index"]]
    assert proposal.meeting_title == event["title"] == "Leadership Meeting"
    assert proposal.priority == event["priority"] == "high"
    assert proposal.meeting_id == "fixture:calendar_events[1]"
    assert proposal.conflict_source == "selected_plan.calendar_conflicts[0]"
    assert proposal.conflict_type == conflict["conflict_type"] == "DEPARTURE_BEFORE_OR_AT_EVENT_START"
    assert proposal.penalty == conflict["penalty"] == -20.0
    assert proposal.current_start == "2026-09-16T16:30:00"
    assert proposal.current_end == "2026-09-16T17:00:00"
    assert proposal.proposed_start == "2026-09-16T15:15:00"
    assert proposal.proposed_end == "2026-09-16T15:45:00"
    assert result["selected_plan"]["feasibility"]["feasible"]
    assert "SOFT PENALTY" in render_demo(report)
    assert "CALENDAR WRITE EXECUTED: NO" in render_demo(report)
    assert "USER APPROVAL REQUIRED" in render_demo(report)
    assert report["calendar_proposals"] == run_demo(traced=False)["calendar_proposals"]


def test_cp3_states_are_fixed_and_no_mutation_api_exists():
    proposal = run_demo()["calendar_proposals"][0]
    assert CALENDAR_MUTATIONS_REQUIRE_EXPLICIT_APPROVAL is True
    assert proposal.approval_required is True
    assert proposal.approval_status == "AWAITING_USER_APPROVAL"
    assert proposal.execution_status == "NOT_EXECUTED"
    assert not {name for name, _ in inspect.getmembers(CalendarChangeProposal, inspect.isfunction)
                if not name.startswith("_")}
    for name, value in (("approval_required", False), ("approval_status", "APPROVED"),
                        ("execution_status", "EXECUTED"), ("proposed_start", "changed")):
        with pytest.raises(FrozenInstanceError):
            setattr(proposal, name, value)
    for name in ("approval_required", "approval_status", "execution_status"):
        with pytest.raises((TypeError, ValueError), match="init=False"):
            replace(proposal, **{name: "override"})


def test_cp3_requires_real_conflict_and_suitable_demo_slot():
    report = run_demo()
    run = report["booked_result"]["run"]
    context, result, scenario = run["context"], run["planning_result"], report["scenario"]
    slots = scenario["calendar_proposal_slots"]
    assert propose_calendar_changes(context, result, [], as_of=scenario["as_of"]) == ()
    without_conflict = deepcopy(result)
    without_conflict["selected_plan"]["calendar_conflicts"] = []
    assert propose_calendar_changes(context, without_conflict, slots, as_of=scenario["as_of"]) == ()
    assert propose_calendar_changes(context, {"selected_plan": None}, slots, as_of=scenario["as_of"]) == ()
    for change in ({"meeting_title": "Other"}, {"allowed_start": "2026-09-16T14:15"},
                   {"allowed_start": "2026-09-16T16:15", "allowed_end": "2026-09-16T16:45"},
                   {"allowed_end": "2026-09-16T15:30"}):
        invalid = [dict(slots[0], **change)]
        assert propose_calendar_changes(context, result, invalid, as_of=scenario["as_of"]) == ()
    mismatched = deepcopy(context)
    mismatched["calendar_events"][1]["title"] = "Different event"
    with pytest.raises(ValueError, match="Conflict does not match"):
        propose_calendar_changes(mismatched, result, slots, as_of=scenario["as_of"])


def test_cp3_never_imports_live_providers_or_model_sdks():
    import builtins
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if (name.startswith("travel_agent.live") or name.split(".")[0] in
                {"openai", "anthropic", "google", "langchain", "langgraph"}):
            raise AssertionError(f"External provider/model import: {name}")
        return original_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=guarded_import):
        report = run_demo()
    assert report["calendar_proposals"][0].execution_status == "NOT_EXECUTED"
