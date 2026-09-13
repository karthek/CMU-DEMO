"""Demo-only dependency injection; production tools, schemas and modules stay frozen."""
from dataclasses import dataclass
from pathlib import Path

from demo.context_evidence import (TransportEvidence, TravelBenefitEvidence, parse_domain, read_json)
from demo.host_evidence import FlightEvidence, parse_evidence, validate_scenario_evidence, EvidenceValidationError
from demo.benefit_critic import BenefitCritic
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.models import TransportEstimate, CalendarEvent, FlightState
from travel_agent.service import TravelService
from travel_agent.itinerary.clock import parse_local


@dataclass(frozen=True)
class DemoEvidence:
    flight: FlightEvidence
    transport: TransportEvidence
    benefit: TravelBenefitEvidence


def load_demo_evidence(scenario, directory=None):
    root = Path(directory or Path(__file__).parent)
    flight = parse_evidence(read_json(root / "recorded_host_evidence.json"))
    validate_scenario_evidence(flight, scenario)
    return DemoEvidence(flight,
        parse_domain(TransportEvidence, read_json(root / "recorded_transport_evidence.json"), scenario),
        parse_domain(TravelBenefitEvidence, read_json(root / "recorded_benefit_evidence.json"), scenario))


class EvidenceFlightTool:
    def __init__(self, evidence, supplemental):
        self.evidence, self.supplemental = evidence, supplemental

    def get_flight(self, flight_number, date):
        e = self.evidence
        if (flight_number, date) != (e.flight_number, e.departure_date):
            return None
        values = dict(self.supplemental, flight_number=e.flight_number,
            origin=e.departure_airport, destination=e.arrival_airport,
            departure_time=e.scheduled_departure)
        return FlightState(**values)


class EvidenceTransportTool:
    def __init__(self, evidence):
        self.evidence = evidence

    def get_estimate(self):
        return TransportEstimate(travel_minutes=self.evidence.estimated_travel_minutes,
            parking_minutes=0, terminal_walk_minutes=0, mode=self.evidence.travel_mode,
            source=self.evidence.source_type)


class DemoCalendarTool:
    def __init__(self, events):
        self.events = tuple(CalendarEvent(**event) for event in events)

    def get_events(self, date):
        return list(self.events)


class BoundedDemoTravelService(TravelService):
    """Bind external MCP context to this validated evidence session, without new APIs."""
    def __init__(self, coordinator, scenario):
        super().__init__(coordinator)
        self.scenario = scenario
        segment = scenario["itineraries"]["records"][0]["segments"][0]
        self.expected_context = self.get_trip_context(segment["flight_number"], segment["departure_date"])

    def evaluate_trip_plans(self, context, candidates=None):
        if context != self.expected_context:
            raise EvidenceValidationError("Context differs from validated demo evidence session")
        if candidates is not None:
            from travel_agent.contracts import validate_candidates
            candidates = validate_candidates(candidates)
            day = parse_local(self.scenario["as_of"]).date()
            if any(parse_local(p["leave_time"]).date() != day
                   or parse_local(p["leave_time"]) < parse_local(self.scenario["as_of"])
                   for p in candidates):
                raise EvidenceValidationError("Candidate outside bounded scenario date/clock")
        return super().evaluate_trip_plans(context, candidates)


def configure_demo_service(service, scenario, evidence):
    coordinator = service.travel_service.coordinator
    coordinator.flight_agent = FlightAgent(EvidenceFlightTool(evidence.flight, scenario["flights"]["flights"][0]))
    coordinator.transport_agent = TransportAgent(EvidenceTransportTool(evidence.transport))
    coordinator.calendar_agent = CalendarAgent(DemoCalendarTool(scenario["calendar_events"]))
    coordinator.planner.critic = BenefitCritic(evidence.benefit, coordinator.planner.policy)
    service.travel_service = BoundedDemoTravelService(coordinator, scenario)
    return service


def render_domains(evidence, result):
    t, b = evidence.transport, evidence.benefit
    lines = ["=== HOST CONTEXT / EVIDENCE DOMAINS ===",
        f"Flight: [{evidence.flight.source_label}] / {evidence.flight.provider.value}",
        f"Origin [USER_SUPPLIED_LOCATION]: {t.origin}",
        f"Transport [{t.source_type}]: {t.destination} | {t.travel_mode} | {t.distance_miles:g} miles | {t.estimated_travel_minutes} minutes",
        "Travel estimate is host-supplied; recorded lookup is not live traffic or a Sep 29 traffic forecast.",
        f"Benefit [{b.source_type}]: {b.benefit_type} | {b.airport} | eligible={b.eligible}",
        f"Facility [metadata only]: {b.facility_name} | {b.facility_location}",
        f"Usable window: {b.usable_from} to {b.usable_until} ({b.time_basis})",
        "Amenities: " + ", ".join(b.amenities),
        "Eligibility is a host assertion conditional on required credentials/boarding pass; admission is not guaranteed.",
        "Security 20 / gate walk 15 minutes [FIXTURE]; calendar and candidate proposals [FIXTURE].",
        "Lounge rule [DEMO]: reserve gate walk before min(boarding, gate deadline); intersect with benefit window.",
        "At least 45 usable minutes: WORKSPACE +12, FOOD +6, RELAXATION +2; cap +20. Infeasible plans receive no utility.",
        "Gate feasibility reports direct arrival; optional lounge use reserves the fixture gate walk before boarding."]
    for plan in result["finalists"]:
        use = plan["score_breakdown"]["lounge_use"]
        lines.append(f"Lounge {plan['leave_time']}: {use['usable_minutes']:g} usable minutes | utility +{use['utility']:.2f} | total {plan['score']:.2f}")
        lines.append("Score components: " + "; ".join(f"{c['code']} {c['value']:+.2f}" for c in plan["score_breakdown"]["components"]))
    return "\n".join(lines)
