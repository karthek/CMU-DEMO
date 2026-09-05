from datetime import datetime, timedelta
from travel_agent.planning.critic import PlanCritic

class BeamSearchPlanner:
    def __init__(self, beam_width=2, depth=3, critic=None):
        self.beam_width = beam_width
        self.depth = depth
        self.critic = critic or PlanCritic()

    def _rank(self, context, plans):
        ranked = []
        for plan in plans:
            item = dict(plan)
            item["score"] = self.critic.score(context, item)
            ranked.append(item)

        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    def search(self, context, initial_candidates):
        beam = self._rank(context, initial_candidates)[: self.beam_width]

        for depth in range(1, self.depth):
            expanded = []
            for plan in beam:
                leave = datetime.fromisoformat(plan["leave_time"])

                for delta, suffix in [(-10, "earlier"), (10, "later")]:
                    revised = leave + timedelta(minutes=delta)
                    new_plan = dict(plan)
                    new_plan["label"] = f"{plan['label']} / {suffix}"
                    new_plan["leave_time"] = revised.isoformat(timespec="minutes")
                    new_plan["summary"] = (
                        f"Leave at {revised.strftime('%H:%M')}; "
                        f"beam refinement depth {depth}."
                    )
                    new_plan["history"] = list(plan.get("history", [])) + [
                        f"Depth {depth}: shifted {abs(delta)} min {suffix}"
                    ]
                    expanded.append(new_plan)

            beam = self._rank(context, expanded)[: self.beam_width]

        return beam
