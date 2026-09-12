"""Active timing model shared by hard feasibility and soft preference scoring."""
from datetime import datetime, timedelta


def modeled_gate_arrival(context, plan) -> datetime:
    return datetime.fromisoformat(plan["leave_time"]) + timedelta(
        minutes=context.airport_travel_minutes + context.security_minutes
        + context.gate_walk_minutes
    )
