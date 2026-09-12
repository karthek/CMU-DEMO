"""Stateless material-change evaluator; never executes planning or emits events."""
from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from travel_agent.live.observations import number, text
from travel_agent.live.time import utc


class TravelState(StrEnum):
    PLANNING = "PLANNING"
    READY_TO_DEPART = "READY_TO_DEPART"
    EN_ROUTE_TO_AIRPORT = "EN_ROUTE_TO_AIRPORT"
    ARRIVED_AT_AIRPORT = "ARRIVED_AT_AIRPORT"
    AIRPORT_MONITORING = "AIRPORT_MONITORING"
    FLIGHT_DEPARTED = "FLIGHT_DEPARTED"


class Decision(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    INFORMATIONAL = "INFORMATIONAL"
    REPLAN = "REPLAN"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class Reason(StrEnum):
    TRAFFIC_DETERIORATED = "TRAFFIC_DETERIORATED"
    RIDESHARE_DETERIORATED = "RIDESHARE_DETERIORATED"
    SECURITY_INCREASED = "SECURITY_INCREASED"
    FLIGHT_SCHEDULE_CHANGED = "FLIGHT_SCHEDULE_CHANGED"
    FLIGHT_DELAYED_EN_ROUTE = "FLIGHT_DELAYED_EN_ROUTE"
    FLIGHT_CANCELLED = "FLIGHT_CANCELLED"
    TERMINAL_CHANGED = "TERMINAL_CHANGED"
    GATE_CHANGED = "GATE_CHANGED"
    CALENDAR_CONFLICT = "CALENDAR_CONFLICT"
    PARKING_UNAVAILABLE = "PARKING_UNAVAILABLE"
    REQUIRED_DATA_RECOVERED = "REQUIRED_DATA_RECOVERED"
    REQUIRED_DATA_UNAVAILABLE = "REQUIRED_DATA_UNAVAILABLE"


@dataclass(frozen=True)
class ReplanningPolicy:
    traffic_deterioration_minutes: int = 10
    rideshare_deterioration_minutes: int = 10
    security_increase_minutes: int = 10
    flight_schedule_change_minutes: int = 15
    same_terminal_gate_buffer_minutes: int = 5

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 0:
                raise ValueError("Non-negative integer replanning policy required")


DEFAULT_REPLANNING_POLICY = ReplanningPolicy()


@dataclass(frozen=True)
class PlanSnapshot:
    """Core-assembled data. missing_required is computed by freshness/applicability policy."""
    segment_id: str
    departure: datetime
    cancelled: bool = False
    terminal: str | None = None
    gate: str | None = None
    road_minutes: float | None = None
    rideshare_pickup_minutes: float | None = None
    rideshare_travel_minutes: float | None = None
    security_minutes: float | None = None
    active_window_conflicts: frozenset[str] = frozenset()
    selected_parking_available: bool | None = None
    parking_price: float | None = None
    rideshare_fare: float | None = None
    missing_required: frozenset[str] = frozenset()

    def __post_init__(self):
        text(self.segment_id)
        object.__setattr__(self, "departure", utc(self.departure))
        if type(self.cancelled) is not bool:
            raise ValueError("Boolean cancellation required")
        if self.selected_parking_available is not None and type(self.selected_parking_available) is not bool:
            raise ValueError("Boolean parking availability required")
        for name in ("road_minutes", "rideshare_pickup_minutes", "rideshare_travel_minutes", "security_minutes", "parking_price", "rideshare_fare"):
            if getattr(self, name) is not None:
                number(getattr(self, name))
        for name in ("active_window_conflicts", "missing_required"):
            value = getattr(self, name)
            if not isinstance(value, frozenset):
                raise ValueError("Immutable identifier set required")
            for item in value:
                text(item)
        for value in (self.terminal, self.gate):
            if value is not None:
                text(value)


@dataclass(frozen=True)
class ChangeEvidence:
    reason: Reason
    delta_minutes: float | None = None


@dataclass(frozen=True)
class ReplanningResult:
    decision: Decision
    replan_required: bool
    reliable_plan_allowed: bool
    evidence: tuple[ChangeEvidence, ...]
    missing_required: tuple[str, ...]
    continue_to_airport: bool


class ReplanningEvaluator:
    def evaluate(self, active_plan: PlanSnapshot, current: PlanSnapshot, *, state: TravelState,
                 previously_unavailable: frozenset[str] = frozenset(), policy=DEFAULT_REPLANNING_POLICY) -> ReplanningResult:
        if active_plan.segment_id != current.segment_id or not isinstance(state, TravelState):
            raise ValueError("Matching segment and typed travel state required")
        if not isinstance(previously_unavailable, frozenset):
            raise ValueError("Immutable recovery state required")
        if state == TravelState.FLIGHT_DEPARTED:
            return ReplanningResult(Decision.NO_CHANGE, False, False, (), (), False)
        en_route = state == TravelState.EN_ROUTE_TO_AIRPORT
        road_active = state in (TravelState.PLANNING, TravelState.READY_TO_DEPART, TravelState.EN_ROUTE_TO_AIRPORT)
        evidence = []
        material = False

        def add(reason, delta=None, *, replan=True):
            nonlocal material
            evidence.append(ChangeEvidence(reason, delta))
            material = material or replan

        def deterioration(before, after, threshold, reason):
            if before is not None and after is not None and after > before and after - before >= threshold:
                add(reason, after - before)

        if road_active:
            deterioration(active_plan.road_minutes, current.road_minutes, policy.traffic_deterioration_minutes, Reason.TRAFFIC_DETERIORATED)
            values = (active_plan.rideshare_pickup_minutes, active_plan.rideshare_travel_minutes,
                      current.rideshare_pickup_minutes, current.rideshare_travel_minutes)
            if all(v is not None for v in values):
                deterioration(values[0] + values[1], values[2] + values[3], policy.rideshare_deterioration_minutes, Reason.RIDESHARE_DETERIORATED)
        deterioration(active_plan.security_minutes, current.security_minutes, policy.security_increase_minutes, Reason.SECURITY_INCREASED)
        shift = (current.departure - active_plan.departure).total_seconds() / 60
        if shift != 0 and abs(shift) >= policy.flight_schedule_change_minutes:
            add(Reason.FLIGHT_DELAYED_EN_ROUTE if en_route and shift > 0 else Reason.FLIGHT_SCHEDULE_CHANGED,
                shift, replan=not (en_route and shift > 0))
        if current.cancelled and not active_plan.cancelled:
            add(Reason.FLIGHT_CANCELLED)
        if active_plan.terminal is not None and current.terminal is not None:
            if active_plan.terminal != current.terminal:
                add(Reason.TERMINAL_CHANGED)
            elif active_plan.gate is not None and current.gate is not None and active_plan.gate != current.gate:
                add(Reason.GATE_CHANGED, replan=False)
        if current.active_window_conflicts - active_plan.active_window_conflicts:
            add(Reason.CALENDAR_CONFLICT)
        if road_active and active_plan.selected_parking_available is True and current.selected_parking_available is False:
            add(Reason.PARKING_UNAVAILABLE)
        missing = set(current.missing_required)
        # Losing an input used by the active plan is not a numeric zero/improvement.
        for field in ("road_minutes", "rideshare_pickup_minutes", "rideshare_travel_minutes", "security_minutes", "selected_parking_available"):
            if field != "security_minutes" and not road_active:
                continue
            if getattr(active_plan, field) is not None and getattr(current, field) is None:
                missing.add(field)
        if missing:
            add(Reason.REQUIRED_DATA_UNAVAILABLE, replan=False)
            return ReplanningResult(Decision.DATA_UNAVAILABLE, material, False, tuple(evidence), tuple(sorted(missing)), en_route)
        if previously_unavailable or active_plan.missing_required:
            add(Reason.REQUIRED_DATA_RECOVERED)
        decision = Decision.REPLAN if material else Decision.INFORMATIONAL if evidence else Decision.NO_CHANGE
        return ReplanningResult(decision, material, not current.cancelled, tuple(evidence), (), en_route)
