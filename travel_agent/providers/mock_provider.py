from datetime import datetime, timedelta
from travel_agent.providers.base import ModelProvider

class MockProvider(ModelProvider):
    def generate_candidate_plans(self, context, count=4):
        departure = datetime.fromisoformat(context.flight.departure_time)
        offsets = [195, 165, 135, 105]
        labels = ["Conservative", "Balanced", "Efficient", "Aggressive"]

        plans = []
        for label, minutes_before in zip(labels, offsets):
            leave = departure - timedelta(minutes=minutes_before)
            plans.append({
                "label": label,
                "leave_time": leave.isoformat(timespec="minutes"),
                "summary": f"Leave at {leave.strftime('%H:%M')} using a {label.lower()} airport strategy.",
                "history": [f"Initial candidate: {label}"],
            })
        return plans[:count]

    def summarize_recommendation(self, context, finalists):
        best = finalists[0]
        leave = datetime.fromisoformat(best["leave_time"])
        return (
            f"Leave at {leave.strftime('%H:%M')} for flight {context.flight.flight_number}. "
            f"Selected '{best['label']}' with score {best['score']:.2f}. "
            f"The planner balanced airport buffer against workday impact."
        )
