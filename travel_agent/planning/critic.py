from datetime import datetime, timedelta

class PlanCritic:
    def score(self, context, plan):
        leave = datetime.fromisoformat(plan["leave_time"])
        departure = datetime.fromisoformat(context.flight.departure_time)
        boarding = datetime.fromisoformat(context.flight.boarding_time)

        airport_arrival = leave + timedelta(minutes=context.airport_travel_minutes)
        gate_ready = airport_arrival + timedelta(
            minutes=context.security_minutes + context.gate_walk_minutes
        )
        safety_margin = (boarding - gate_ready).total_seconds() / 60

        score = 100.0

        if safety_margin < 0:
            score -= 100
        elif safety_margin < 20:
            score -= 35
        elif safety_margin < context.preferred_buffer_minutes:
            score -= 10

        minutes_before_departure = (departure - leave).total_seconds() / 60
        score -= max(0, minutes_before_departure - 120) * 0.08

        for event in context.calendar_events:
            event_start = datetime.fromisoformat(event.start)
            event_end = datetime.fromisoformat(event.end)

            if event_start <= leave < event_end:
                score -= 60 if event.priority == "high" else 25

            if event.priority == "high" and leave <= event_start:
                score -= 20

        return round(score, 2)
