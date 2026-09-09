"""Both activation paths converge on the unchanged V7 TravelService."""
import sqlite3
from travel_agent.agents.errors import DataUnavailableError, InvalidDataError, ToolFailureError
from travel_agent.contracts import validate_candidates
from travel_agent.itinerary.activation import ActivationEvaluator
from travel_agent.itinerary.clock import TIME_BASIS, parse_local, validate_local
from travel_agent.itinerary.contracts import validate_selector
from travel_agent.itinerary.locking import MonitorBusy, MonitorLock
from travel_agent.itinerary.models import (AutomaticDispatch, Diagnostic, ManualResult, MonitorResult,
                                           PlanningProvenance)
from travel_agent.itinerary.policy import DEFAULT_ACTIVATION_POLICY


class ConsistencyError(Exception):
    def __init__(self, code):
        self.code = code


class ItineraryService:
    def __init__(self, agent, repository, travel_service, clock, *, policy=DEFAULT_ACTIVATION_POLICY):
        self.agent = agent
        self.repository = repository
        self.travel_service = travel_service
        self.clock = clock
        self.policy = policy
        self.evaluator = ActivationEvaluator()

    def monitor_trips(self):
        as_of = validate_local(self.clock.now())
        try:
            with MonitorLock(self.repository.path):
                self.repository.recover_interrupted(as_of=as_of)
                refresh = self.repository.apply_snapshot(self.agent.refresh(), as_of=as_of)
                if refresh.status != "APPLIED":
                    return MonitorResult(as_of, TIME_BASIS, refresh.to_dict(), (), (), (), refresh.diagnostics).to_dict()
                activations, dispatches, activated = [], [], []
                for segment in self.repository.list_current_segments(source_id=self.agent.source_id):
                    self.repository.initialize_automatic_state(segment.segment_id, as_of=as_of)
                    activation = self.evaluator.evaluate(segment, as_of, self.policy)
                    activations.append(activation.to_dict())
                    before = self.repository.automatic_state(segment.segment_id)["state"]
                    revision = self.repository.revision(segment.segment_id)
                    attempt = None
                    if not activation.eligible:
                        decision = "NOT_ELIGIBLE"
                    elif before == "COMPLETED":
                        decision = "SUPPRESS_DUPLICATE"
                        attempt = self.repository.completed_attempt(segment.segment_id).to_dict()
                    elif before in ("NOT_ACTIVATED", "FAILED"):
                        decision = "RETRY" if before == "FAILED" else "PLAN"
                        activated.append(segment)
                        attempt = self._plan_segment(segment, activation, as_of, "AUTOMATIC").to_dict()
                    else:
                        # With the lock and recovery above this indicates a broken state invariant.
                        raise RuntimeError("Unexpected active automatic execution")
                    after = self.repository.automatic_state(segment.segment_id)["state"]
                    dispatches.append(AutomaticDispatch(segment.itinerary_id, segment.segment_id, decision,
                                                        before, after, revision, activation.to_dict(), attempt))
                return MonitorResult(as_of, TIME_BASIS, refresh.to_dict(), tuple(activations),
                                     tuple(dispatches), tuple(activated), ()).to_dict()
        except MonitorBusy:
            return MonitorResult(as_of, TIME_BASIS, None, (), (), (),
                                 (Diagnostic("MONITOR_BUSY", "Another itinerary invocation is active."),)).to_dict()

    def plan_booked_trip(self, *, selector, candidates=None):
        selector = validate_selector(selector)
        if candidates is not None:
            candidates = validate_candidates(candidates)
            for candidate in candidates:
                parse_local(candidate["leave_time"])
        as_of = validate_local(self.clock.now())
        try:
            # Share the lock so a manual refresh cannot replace a monitor's booking snapshot.
            # Manual execution never consults or mutates automatic execution state.
            with MonitorLock(self.repository.path):
                refresh = self.repository.apply_snapshot(self.agent.refresh(), as_of=as_of)
                if refresh.status != "APPLIED":
                    return ManualResult(as_of, TIME_BASIS, refresh.to_dict(), "REFRESH_BLOCKED", (), None, refresh.diagnostics).to_dict()
                matches = self.repository.resolve_upcoming(selector, as_of=as_of, source_id=self.agent.source_id)
                if len(matches) != 1:
                    status = "NOT_FOUND" if not matches else "NEEDS_SELECTION"
                    code = "NO_MATCHING_UPCOMING_SEGMENT" if not matches else "MULTIPLE_MATCHING_SEGMENTS"
                    return ManualResult(as_of, TIME_BASIS, refresh.to_dict(), status, matches, None,
                                        (Diagnostic(code, "Selector did not resolve exactly one upcoming segment."),)).to_dict()
                segment = matches[0]
                activation = self.evaluator.evaluate(segment, as_of, self.policy)
                attempt = self._plan_segment(segment, activation, as_of, "USER_INITIATED", candidates)
                return ManualResult(as_of, TIME_BASIS, refresh.to_dict(), attempt.state, matches, attempt.to_dict(), ()).to_dict()
        except MonitorBusy:
            return ManualResult(as_of, TIME_BASIS, None, "BUSY", (), None,
                                (Diagnostic("MONITOR_BUSY", "Another itinerary invocation is active."),)).to_dict()

    def _plan_segment(self, segment, activation, as_of, mode, candidates=None):
        trigger = "AUTOMATIC_LEAD_TIME" if mode == "AUTOMATIC" else "USER_REQUEST"
        provenance = PlanningProvenance(mode, trigger, as_of, segment.itinerary_id, segment.segment_id,
                                        self.repository.revision(segment.segment_id))
        # Commit PLANNING before any operational lookup or V7 evaluation.
        attempt_id = self.repository.start_attempt(segment, activation, provenance)
        context = result = error = None
        try:
            retrieved = self.travel_service.get_trip_context(segment.flight_number, segment.departure_date)
            flight = retrieved["flight"]
            departure = parse_local(flight["departure_time"])
            parse_local(flight["boarding_time"])
            if flight["flight_number"] != segment.flight_number or departure.date().isoformat() != segment.departure_date:
                raise ConsistencyError("BOOKING_FLIGHT_IDENTITY_MISMATCH")
            if (flight["origin"], flight["destination"]) != (segment.origin, segment.destination):
                raise ConsistencyError("BOOKING_FLIGHT_ROUTE_MISMATCH")
            for event in retrieved["calendar_events"]:
                parse_local(event["start"])
                parse_local(event["end"])
            context = retrieved
            result = self.travel_service.evaluate_trip_plans(context, candidates)
            if result.get("status") not in ("PLAN_FOUND", "NO_FEASIBLE_PLAN"):
                raise RuntimeError("Invalid planner outcome")
        except sqlite3.Error:
            raise
        except Exception as exc:
            if isinstance(exc, ConsistencyError):
                code = exc.code
            elif isinstance(exc, DataUnavailableError):
                code = "OPERATIONAL_FLIGHT_UNAVAILABLE" if exc.agent == "flight" else "OPERATIONAL_LOOKUP_FAILED"
            elif isinstance(exc, InvalidDataError):
                code = ("BOOKING_FLIGHT_IDENTITY_MISMATCH" if exc.agent == "flight" and exc.message in (
                    "Returned flight number does not match request.", "Returned departure date does not match request.")
                        else "OPERATIONAL_DATA_INVALID")
            elif isinstance(exc, ToolFailureError):
                code = ("OPERATIONAL_DATA_INVALID" if isinstance(exc.__cause__, (ValueError, TypeError, KeyError))
                        else "OPERATIONAL_LOOKUP_FAILED")
            elif isinstance(exc, ValueError):
                code = "OPERATIONAL_DATA_INVALID"
            else:
                code = "PLANNING_FAILED"
            result = None
            error = Diagnostic(code, "Booked segment planning could not complete.",
                               itinerary_id=segment.itinerary_id, segment_id=segment.segment_id).to_dict()
        # The single invocation time is also the audit timestamp: no second hidden clock read.
        return self.repository.finish_attempt(attempt_id, finished_at=as_of, context=context, result=result, error=error)
