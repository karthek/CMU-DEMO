"""Hard gate feasibility; independent of Critic preference scores."""
from datetime import datetime, timedelta

from travel_agent.planning.models import FeasibilityResult, HardConstraintFailure
from travel_agent.planning.policy import DEFAULT_PLANNING_POLICY
from travel_agent.planning.timing import modeled_gate_arrival


def evaluate_feasibility(context, candidate, policy=DEFAULT_PLANNING_POLICY) -> FeasibilityResult:
    arrival = modeled_gate_arrival(context, candidate)
    deadline = datetime.fromisoformat(context.flight.departure_time) - timedelta(
        minutes=policy.gate_close_buffer_minutes
    )
    failures = () if arrival <= deadline else (
        HardConstraintFailure("GATE_DEADLINE_MISSED", (arrival - deadline).total_seconds() / 60),
    )
    return FeasibilityResult(
        feasible=not failures, gate_arrival_time=arrival.isoformat(),
        gate_deadline=deadline.isoformat(),
        gate_close_buffer_minutes=policy.gate_close_buffer_minutes,
        deadline_source="configured_policy", hard_constraint_failures=failures,
    )
