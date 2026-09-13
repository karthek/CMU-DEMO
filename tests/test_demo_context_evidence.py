"""Demo evidence boundaries, actual server injection, generic utility and privacy."""
import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from demo import host_evidence_demo as host
from demo.benefit_critic import BenefitCritic, lounge_use
from demo.cmu_demo import load_scenario, run_demo
from demo.composition import load_demo_evidence, EvidenceTransportTool
from demo.context_evidence import TransportEvidence, TravelBenefitEvidence, parse_domain, read_json
from demo.host_evidence import EvidenceValidationError
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.contracts import context_from_dict
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.planning.critic import PlanCritic


def domain_payload(name):
    return read_json(host.ROOT / f"demo/recorded_{name}_evidence.json")


@pytest.fixture
def context():
    return context_from_dict(run_demo()["booked_result"]["run"]["context"])


def candidate(time):
    return dict(label="Host proposal", leave_time=f"2026-09-29T{time}", summary="Evaluate timing")


@pytest.mark.parametrize("changes", [
    {"estimated_travel_minutes": -1}, {"estimated_travel_minutes": 0},
    {"estimated_travel_minutes": True}, {"estimated_travel_minutes": 33.5},
    {"estimated_travel_minutes": "33"}, {"estimated_travel_minutes": 1441},
    {"destination": "ATL"}, {"origin": ""}, {"origin": "Different location"},
    {"origin": {"address": "invalid"}}, {"travel_date": "2026-09-28"},
    {"time_basis": "UTC"}, {"distance_miles": -1}, {"distance_miles": float("nan")},
    {"distance_miles": float("inf")}, {"distance_miles": True},
    {"source_reference": "bad\noutput"}, {"retrieved_on": "2026-09-30"},
    {"unknown": 1}, {"score": 999}, {"approval_status": "APPROVED"},
])
def test_invalid_transport_stops_before_mcp(changes):
    payload = dict(domain_payload("transport"), **changes)
    with patch.object(host, "Client") as client, pytest.raises(EvidenceValidationError):
        asyncio.run(host.rehearse_evidence(transport_payload=payload))
    client.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"airport": "ATL"}, {"usable_from": "bad"},
    {"usable_from": "2026-09-29T18:00"}, {"usable_until": "2026-09-29T14:00"},
    {"usable_from": "2026-09-28T15:00"}, {"usable_until": "2026-09-29T18:01"},
    {"usable_until": "2026-09-29T18:00Z"}, {"eligible": "true"}, {"eligible": 1},
    {"amenities": "FOOD"}, {"amenities": ["FOOD", "FOOD"]},
    {"amenities": ["UNKNOWN"]}, {"amenities": [{}]}, {"benefit_type": "CARD"},
    {"score": 999}, {"card_number": "not accepted"}, {"time_basis": "UTC"},
])
def test_invalid_benefit_stops_before_mcp(changes):
    payload = dict(domain_payload("benefit"), **changes)
    with patch.object(host, "Client") as client, pytest.raises(EvidenceValidationError):
        asyncio.run(host.rehearse_evidence(benefit_payload=payload))
    client.assert_not_called()


@pytest.mark.parametrize("domain,kind", [("transport", TransportEvidence), ("benefit", TravelBenefitEvidence)])
def test_all_domain_fields_required(domain, kind):
    payload = domain_payload(domain)
    for field in payload:
        missing = dict(payload)
        del missing[field]
        with pytest.raises(EvidenceValidationError):
            parse_domain(kind, missing, load_scenario())


@pytest.mark.parametrize("time,minutes,points", [
    ("15:30", 52, 20), ("15:37", 45, 20), ("15:37:01", 44 + 59/60, 0),
    ("15:38", 44, 0), ("16:30", 0, 0), ("18:00", 0, 0),
])
def test_utility_exact_threshold_and_infeasible(context, time, minutes, points):
    benefit = load_demo_evidence(load_scenario()).benefit
    use = lounge_use(context, candidate(time), benefit)
    assert use["usable_minutes"] == pytest.approx(minutes)
    assert use["utility"] == points
    result = BeamSearchPlanner(critic=BenefitCritic(benefit)).search_outcome(context, [candidate(time)])
    if time == "18:00":
        assert not result.finalists
        assert "score" not in result.diagnostics["best_infeasible_candidate"]


@pytest.mark.parametrize("amenities,points", [((), 0), (("FOOD",), 6), (("WORKSPACE",), 12),
    (("RELAXATION",), 2), (("FOOD", "WORKSPACE"), 18), (("FOOD", "WORKSPACE", "RELAXATION"), 20)])
def test_exact_generic_amenity_contributions(context, amenities, points):
    benefit = replace(load_demo_evidence(load_scenario()).benefit, amenities=amenities)
    plan = candidate("15:30")
    base = PlanCritic().evaluate(context, plan)
    actual = BenefitCritic(benefit).evaluate(context, plan)
    assert actual.score == round(base.score + points, 2)
    assert actual.calendar_conflicts == base.calendar_conflicts
    assert actual.score_breakdown["components"][:-1] == base.score_breakdown["components"]


def test_intersection_clamps_start_and_end_and_eligibility(context):
    benefit = load_demo_evidence(load_scenario()).benefit
    assert lounge_use(context, candidate("13:30"), benefit)["usable_start"] == "2026-09-29T15:00:00"
    assert lounge_use(context, candidate("13:30"), benefit)["usable_minutes"] == 135
    assert lounge_use(context, candidate("15:30"), replace(benefit, eligible=False))["utility"] == 0
    assert lounge_use(context, candidate("15:30"), replace(benefit, usable_until="2026-09-29T16:40"))["utility"] == 0
    late_boarding = replace(context, flight=replace(context.flight, boarding_time="2026-09-29T18:00"))
    assert lounge_use(late_boarding, candidate("15:30"), benefit)["usable_end"] == "2026-09-29T17:30:00"


def test_benefit_can_change_choice_without_changing_feasibility(context):
    # Isolate the generic productivity tradeoff from separate calendar/buffer penalties.
    context = replace(context, calendar_events=[], preferred_buffer_minutes=0)
    proposals = [candidate("15:30"), candidate("15:40")]
    benefit = load_demo_evidence(load_scenario()).benefit
    plain = BeamSearchPlanner(depth=1).search(context, proposals)
    enriched = BeamSearchPlanner(depth=1, critic=BenefitCritic(benefit)).search(context, proposals)
    assert plain[0]["leave_time"].endswith("15:40")
    assert enriched[0]["leave_time"].endswith("15:30")
    assert {p["leave_time"]: p["feasibility"] for p in plain} == {p["leave_time"]: p["feasibility"] for p in enriched}


def test_brand_neutrality_real_mcp():
    baseline = asyncio.run(host.rehearse_evidence())
    payload = domain_payload("benefit")
    payload.update(facility_name="Example Airport Workspace", facility_location="Example connector",
        provenance_label="Fictional employer travel entitlement", source_reference="https://example.org/benefits",
        source_type="HOST_TRAVEL_BENEFIT_EVIDENCE")
    other = asyncio.run(host.rehearse_evidence(benefit_payload=payload))
    assert baseline["planning_result"] == other["planning_result"]
    assert baseline["proposals"] == other["proposals"]


def test_external_transport_crosses_agent_context_and_changes_feasibility():
    payload = domain_payload("transport")
    payload.update(source_type="HOST_TRAVEL_EVIDENCE", estimated_travel_minutes=150)
    typed = parse_domain(TransportEvidence, payload, load_scenario())
    assert TransportAgent(EvidenceTransportTool(typed)).get_estimate().travel_minutes == 150
    report = asyncio.run(host.rehearse_evidence(transport_payload=payload))
    assert report["context"]["airport_travel_minutes"] == 150
    assert report["planning_result"]["status"] == "NO_FEASIBLE_PLAN"
    assert report["planning_result"]["selected_plan"] is None
    assert report["proposals"] == ()


def test_all_external_domains_cli_outside_repo(capsys):
    with TemporaryDirectory(prefix="cmu-external-test-") as directory:
        root = Path(directory)
        assert not root.resolve().is_relative_to(host.ROOT.resolve())
        flight = dict(host.load_payload(), source_type="LIVE_HOST_EVIDENCE")
        transport = dict(domain_payload("transport"), source_type="HOST_TRAVEL_EVIDENCE", estimated_travel_minutes=40)
        benefit = dict(domain_payload("benefit"), source_type="HOST_TRAVEL_BENEFIT_EVIDENCE")
        args = []
        for flag, name, payload in (("--evidence", "flight", flight), ("--transport-evidence", "transport", transport),
                                     ("--benefit-evidence", "benefit", benefit)):
            path = root / f"{name}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            args += [flag, str(path)]
        assert host.main(args) == 0
        output = capsys.readouterr()
        assert output.err == ""
        for value in ("LIVE HOST EVIDENCE", "MCP TRANSPORT: STDIO", "40 minutes", "HOST_TRAVEL_BENEFIT_EVIDENCE",
                      "45 usable minutes", "AWAITING_USER_APPROVAL", "NOT_EXECUTED", "CALENDAR WRITE EXECUTED: NO"):
            assert value in output.out


@pytest.mark.parametrize("flag", ["--transport-evidence", "--benefit-evidence"])
@pytest.mark.parametrize("contents", ["null", "[]", "{}", '{"eligible":true,"eligible":false}'])
def test_invalid_external_json_never_uses_recorded_defaults(flag, contents, tmp_path, capsys):
    path = tmp_path / "invalid.json"
    path.write_text(contents, encoding="utf-8")
    with patch.object(host, "Client") as client:
        assert host.main([flag, str(path)]) == 1
    client.assert_not_called()
    output = capsys.readouterr()
    assert not output.out and "no fallback" in output.err


def test_demo_scope_rejects_candidate_date_and_context_override():
    with pytest.raises(RuntimeError, match="Demo planning did not complete: FAILED"):
        run_demo(candidates=[dict(candidate("15:30"), leave_time="2026-09-28T15:30")])


def test_all_existing_agents_actually_participate():
    calls = []
    def spy(kind, method):
        original = getattr(kind, method)
        def invoke(self, *args, **kwargs):
            calls.append(kind.__name__)
            return original(self, *args, **kwargs)
        return invoke
    with patch.object(FlightAgent, "get_flight", spy(FlightAgent, "get_flight")), \
         patch.object(TransportAgent, "get_estimate", spy(TransportAgent, "get_estimate")), \
         patch.object(CalendarAgent, "get_events", spy(CalendarAgent, "get_events")):
        run_demo()
    assert set(calls) == {"FlightAgent", "TransportAgent", "CalendarAgent"}


def test_actual_mcp_rejects_session_context_or_date_tampering():
    from mcp.client import Client
    from demo.mcp_host_demo import server_parameters
    async def exercise():
        async with Client(server_parameters()) as client:
            response = await client.call_tool("get_trip_context", {"flight_number": "AA2983", "date": "2026-09-29"})
            context = response.structured_content
            changed = dict(context, airport_travel_minutes=0)
            result = await client.call_tool("evaluate_trip_plans", {"context": changed, "candidates": [candidate("15:30")]})
            assert result.is_error
            wrong_date = dict(candidate("15:30"), leave_time="2026-09-28T15:30")
            result = await client.call_tool("evaluate_trip_plans", {"context": context, "candidates": [wrong_date]})
            assert result.is_error
    asyncio.run(exercise())


def test_recorded_payloads_have_only_allowed_fields():
    flight = host.load_payload()
    assert set(flight) == {"source_type", "provider", "flight_number", "departure_airport", "arrival_airport",
        "departure_date", "scheduled_departure", "scheduled_arrival", "time_basis", "evidence_timestamp"}
    for domain, kind in (("transport", TransportEvidence), ("benefit", TravelBenefitEvidence)):
        parse_domain(kind, domain_payload(domain), load_scenario())
