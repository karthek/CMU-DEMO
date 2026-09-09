from datetime import datetime

from travel_agent.planning.models import ScoreEvaluation
from travel_agent.planning.timing import modeled_gate_arrival


class PlanCritic:
    def score(self, context, plan):
        return self.evaluate(context, plan).score

    def evaluate(self, context, plan) -> ScoreEvaluation:
        leave = datetime.fromisoformat(plan["leave_time"])
        departure = datetime.fromisoformat(context.flight.departure_time)
        boarding = datetime.fromisoformat(context.flight.boarding_time)
        safety_margin = (boarding - modeled_gate_arrival(context, plan)).total_seconds() / 60
        components = [{"code": "BASE_SCORE", "value": 100.0}]
        boarding_penalty = 0.0
        if safety_margin < 0:
            boarding_penalty = -100.0
        elif safety_margin < 20:
            boarding_penalty = -35.0
        elif safety_margin < context.preferred_buffer_minutes:
            boarding_penalty = -10.0
        components.append({"code": "BOARDING_BUFFER", "value": boarding_penalty})
        minutes_before_departure = (departure - leave).total_seconds() / 60
        components.append({"code": "EARLY_DEPARTURE",
                           "value": -max(0, minutes_before_departure - 120) * 0.08})
        conflicts = []

        def calendar_penalty(event, event_index, code, conflict_type, penalty):
            conflicts.append({
                "event_index": event_index, "title": event.title,
                "start": datetime.fromisoformat(event.start).isoformat(),
                "end": datetime.fromisoformat(event.end).isoformat(),
                "priority": event.priority, "conflict_type": conflict_type,
                "penalty": penalty, "score_component_index": len(components),
            })
            components.append({"code": code, "value": penalty, "event_index": event_index})

        for index, event in enumerate(context.calendar_events):
            event_start = datetime.fromisoformat(event.start)
            event_end = datetime.fromisoformat(event.end)
            if event_start <= leave < event_end:
                calendar_penalty(event, index, "DEPARTURE_DURING_EVENT",
                                 "DEPARTURE_DURING_EVENT", -60.0 if event.priority == "high" else -25.0)
            # Independent rule: at a high-priority event's start both deductions apply.
            if event.priority == "high" and leave <= event_start:
                calendar_penalty(event, index, "HIGH_PRIORITY_EVENT_AHEAD",
                                 "DEPARTURE_BEFORE_OR_AT_EVENT_START", -20.0)
        return ScoreEvaluation(
            round(sum(c["value"] for c in components), 2),
            {"components": components, "rounding": "round(sum(component values), 2)"},
            conflicts,
        )
