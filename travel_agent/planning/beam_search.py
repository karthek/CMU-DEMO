from datetime import datetime, timedelta
from travel_agent.planning.critic import PlanCritic

class BeamSearchPlanner:
    def __init__(self, beam_width=2, depth=3, critic=None):
        for name, value in (("beam_width", beam_width), ("depth", depth)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.beam_width = beam_width
        self.depth = depth
        self.critic = critic or PlanCritic()

    def _rank(self, context, plans):
        ranked = []
        seen = set()
        for plan in plans:
            # Equivalent ISO spellings (16:15 and 16:15:00) share one slot.
            leave = datetime.fromisoformat(plan["leave_time"])
            if leave in seen:
                continue
            seen.add(leave)
            item = dict(plan)
            item["score"] = self.critic.score(context, item)
            ranked.append(item)

        # Stable sort: first encountered wins ties. Parents precede children;
        # children follow beam order, earlier then later. Keep real histories.
        ranked.sort(key=lambda x: x["score"], reverse=True)
        return ranked

    def search(self, context, initial_candidates):
        if not initial_candidates:
            raise ValueError("initial_candidates must not be empty")
        beam = self._rank(context, initial_candidates)[: self.beam_width]

        for depth in range(1, self.depth):
            expanded = list(beam)
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
