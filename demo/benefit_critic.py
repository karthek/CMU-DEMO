"""Generic demo utility injected into the unchanged production Beam Search."""
from datetime import datetime, timedelta

from travel_agent.planning.critic import PlanCritic
from travel_agent.planning.feasibility import evaluate_feasibility
from travel_agent.planning.models import ScoreEvaluation
from travel_agent.planning.policy import DEFAULT_PLANNING_POLICY

MIN_USEFUL_MINUTES = 45
AMENITY_POINTS = {"WORKSPACE": 12.0, "FOOD": 6.0, "RELAXATION": 2.0}
MAX_UTILITY = 20.0


def lounge_use(context, plan, benefit, policy=DEFAULT_PLANNING_POLICY):
    """Reserve gate walking before boarding/deadline; no overlap or infeasible => zero."""
    feasibility = evaluate_feasibility(context, plan, policy)
    post_security = datetime.fromisoformat(plan["leave_time"]) + timedelta(
        minutes=context.airport_travel_minutes + context.security_minutes)
    end = min(datetime.fromisoformat(context.flight.boarding_time),
              datetime.fromisoformat(feasibility.gate_deadline)) - timedelta(minutes=context.gate_walk_minutes)
    start = max(post_security, datetime.fromisoformat(benefit.usable_from))
    end = min(end, datetime.fromisoformat(benefit.usable_until))
    applicable = (benefit.eligible and benefit.benefit_type == "AIRPORT_LOUNGE"
                  and benefit.airport == context.flight.origin and feasibility.feasible)
    minutes = max(0.0, (end - start).total_seconds() / 60) if applicable else 0.0
    utility = min(MAX_UTILITY, sum(AMENITY_POINTS[a] for a in benefit.amenities)) if minutes >= MIN_USEFUL_MINUTES else 0.0
    return {"usable_minutes": minutes, "utility": utility,
            "post_security": post_security.isoformat(), "usable_start": start.isoformat(),
            "usable_end": end.isoformat(), "minimum_minutes": MIN_USEFUL_MINUTES}


class BenefitCritic(PlanCritic):
    def __init__(self, benefit, policy=DEFAULT_PLANNING_POLICY):
        self.benefit, self.policy = benefit, policy

    def evaluate(self, context, plan):
        base = super().evaluate(context, plan)
        use = lounge_use(context, plan, self.benefit, self.policy)
        components = base.score_breakdown["components"] + [
            {"code": "AIRPORT_LOUNGE_UTILITY", "value": use["utility"]}]
        return ScoreEvaluation(round(sum(c["value"] for c in components), 2),
            {"components": components, "rounding": "round(sum(component values), 2)",
             "lounge_use": use}, base.calendar_conflicts)
