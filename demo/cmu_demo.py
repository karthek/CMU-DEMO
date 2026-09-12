"""Offline demo with observational search trace. Run: python -B demo/cmu_demo.py."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

# Support direct script launch without installation or a caller-supplied PYTHONPATH.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock, TIME_BASIS, parse_local
from demo.search_trace import TracedBeamSearchPlanner, render_trace
from demo.hitl import propose_calendar_changes, render_hitl


def load_scenario(path=None):
    return json.loads(Path(path or Path(__file__).with_name("scenario.json")).read_text(encoding="utf-8"))


def run_demo(*, traced=True, candidates=None):
    """Return real service evidence/results; all persistence is temporary."""
    scenario = load_scenario()
    if candidates is not None:
        # Demo rehearsal input override only; production service still validates.
        from copy import deepcopy
        scenario["candidates"] = deepcopy(candidates)
    with TemporaryDirectory(prefix="cmu-demo-") as directory:
        root = Path(directory)
        for name in ("itineraries", "flights"):
            (root / f"{name}.json").write_text(json.dumps(scenario[name]), encoding="utf-8")
        service = create_itinerary_service(
            database_path=root / "demo.sqlite3",
            itinerary_path=root / "itineraries.json",
            flight_path=root / "flights.json",
            clock=FixedClock(parse_local(scenario["as_of"])),
        )
        try:
            planner = service.travel_service.coordinator.planner
            if traced:
                planner = TracedBeamSearchPlanner(beam_width=planner.beam_width, depth=planner.depth,
                                                 critic=planner.critic, policy=planner.policy)
                service.travel_service.coordinator.planner = planner
            result = service.plan_booked_trip(selector={}, candidates=scenario["candidates"])
            if result["status"] != "COMPLETED":
                raise RuntimeError(f"Demo planning did not complete: {result['status']}")
            proposals = propose_calendar_changes(
                result["run"]["context"], result["run"]["planning_result"],
                scenario.get("calendar_proposal_slots", []), as_of=scenario["as_of"])
            return {"scenario": scenario, "booked_result": result,
                    "beam_width": planner.beam_width, "depth": planner.depth,
                    "calendar_proposals": proposals,
                    "trace": planner.stages if traced else []}
        finally:
            service.repository.close()


def render_demo(report):
    scenario = report["scenario"]
    run = report["booked_result"]["run"]
    context = run["context"]
    result = run["planning_result"]
    flight = context["flight"]
    selected = result["selected_plan"]
    lines = ["=== CMU TRAVEL AGENT DEMO ===", "", "MODE",
             "Offline fixture mode: no network, Gmail, Google Calendar, or LLM.",
             f"Fixed scenario clock [FIXTURE]: {scenario['as_of']} ({TIME_BASIS})",
             "", "USER GOAL", scenario["user_goal"], "", "BOOKED TRIP [FIXTURE]"]
    for record in scenario["itineraries"]["records"]:
        for segment in record["segments"]:
            lines.append(f"{record['itinerary_id']} / {segment['segment_id']}: "
                         f"{segment['flight_number']} {segment['origin']} -> {segment['destination']}, "
                         f"{segment['scheduled_departure']} ({segment['booking_status']})")
    lines += ["", "CALENDAR CONTEXT [FIXTURE]"]
    for event in context["calendar_events"]:
        lines.append(f"{event['title']}: {event['start']} to {event['end']} ({event['priority']})")
    lines += ["", "AGENT INPUTS [FIXTURE]",
              f"Flight: {flight['flight_number']}; departure {flight['departure_time']}; "
              f"boarding {flight['boarding_time']}; gate {flight['gate']}; {flight['status']}",
              "Calendar: existing CalendarAgent reads FakeCalendarTool workday events.",
              f"Transport: {context['airport_travel_minutes']} minutes driving to ATL (FakeTransportTool).",
              f"Timing used by core: travel {context['airport_travel_minutes']} + "
              f"security {context['security_minutes']} + gate walk {context['gate_walk_minutes']} minutes.",
              f"Preferred boarding buffer (soft score): {context['preferred_buffer_minutes']} minutes.",
              "", "HOST-STYLE CANDIDATES", "Rehearsed proposals, not live model output:"]
    for candidate in scenario["candidates"]:
        lines.append(f"{candidate['label']}: {candidate['leave_time']} - {candidate['summary']}")
    if report["trace"]:
        lines += ["", render_trace(report["trace"], report["beam_width"], report["depth"], result["finalists"])]
    lines += ["", "DETERMINISTIC EVALUATION",
              "ItineraryService -> TravelService -> Coordinator -> agents / existing planner.",
              f"Beam width {report['beam_width']}, depth {report['depth']} (two refinement rounds).",
              f"Status: {result['status']}; evaluated candidates: {result['diagnostics']['evaluated_candidate_count']}"]
    for index, plan in enumerate(result["finalists"], 1):
        feasibility = plan["feasibility"]
        lines.append(f"{index}. {plan['leave_time']} | score {plan['score']:.2f} | "
                     f"gate feasible: {feasibility['feasible']} | modeled gate arrival {feasibility['gate_arrival_time']}")
    lines += ["", "RECOMMENDED PLAN", "Best within explored candidates:", result["recommendation"], "", "WHY"]
    if selected:
        feasibility = selected["feasibility"]
        lines.append(f"Configured gate buffer: {feasibility['gate_close_buffer_minutes']} minutes; "
                     f"gate deadline: {feasibility['gate_deadline']}.")
        for component in selected["score_breakdown"]["components"]:
            lines.append(f"{component['code']}: {component['value']:+.2f}")
        for conflict in selected["calendar_conflicts"]:
            lines.append(f"Calendar soft penalty: {conflict['title']} / {conflict['conflict_type']} "
                         f"({conflict['penalty']:+.2f})")
    if report["calendar_proposals"]:
        lines += ["", render_hitl(report["calendar_proposals"])]
    lines += ["", "BOUNDARIES / ASSUMPTIONS",
              "All trip, calendar, and transport evidence is FIXTURE data; local times use America/New_York.",
              "Gate feasibility is relative to configured timing assumptions, not proof of real-world arrival.",
              "Calendar conflicts are soft scoring penalties, not hard gate infeasibility.",
              "Parking and terminal-walking components are not added to the production timing calculation.",
              "No calendar mutation or approval/execution workflow is available in this demo.",
              "Lounge / grab-and-go behavior is a later demo enhancement; no lounge optimization is performed.",
              "Runtime state is isolated in a temporary directory and removed after each run."]
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_demo(run_demo()))
