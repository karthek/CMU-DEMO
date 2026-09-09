from datetime import datetime, timedelta
from travel_agent.planning.critic import PlanCritic
from travel_agent.planning.feasibility import evaluate_feasibility
from travel_agent.planning.models import SearchOutcome
from travel_agent.planning.policy import DEFAULT_PLANNING_POLICY

class BeamSearchPlanner:
    def __init__(self, beam_width=2, depth=3, critic=None, *, policy=DEFAULT_PLANNING_POLICY):
        for name, value in (("beam_width", beam_width), ("depth", depth)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.beam_width = beam_width
        self.depth = depth
        self.critic = critic or PlanCritic()
        self.policy = policy

    def _rank(self, context, plans, evaluated=None):
        feasible = []
        infeasible = []
        seen = set()
        for plan in plans:
            # Equivalent ISO spellings (16:15 and 16:15:00) share one slot.
            leave = datetime.fromisoformat(plan["leave_time"])
            if leave in seen:
                continue
            seen.add(leave)
            # Only proposal fields survive; never inherit evaluations from a parent/host.
            item = {key: plan[key] for key in ("label", "leave_time", "summary")}
            item["history"] = list(plan.get("history", []))
            item["feasibility"] = evaluate_feasibility(context, item, self.policy).to_dict()
            if item["feasibility"]["feasible"]:
                item.update(self.critic.evaluate(context, item).to_dict())
                feasible.append(item)
            else:
                infeasible.append(item)
            if evaluated is not None:
                evaluated.setdefault(leave, item)

        # Stable sort: first encountered wins ties. Parents precede children;
        # children follow beam order, earlier then later. Keep real histories.
        feasible.sort(key=lambda x: x["score"], reverse=True)
        infeasible.sort(key=self._minutes_late)
        return feasible + infeasible

    @staticmethod
    def _minutes_late(plan):
        return plan["feasibility"]["hard_constraint_failures"][0]["minutes_late"]

    def search(self, context, initial_candidates):
        """Compatibility list API: only feasible recommended finalists."""
        return self.search_outcome(context, initial_candidates).finalists

    def search_outcome(self, context, initial_candidates) -> SearchOutcome:
        if not initial_candidates:
            raise ValueError("initial_candidates must not be empty")
        evaluated = {}
        beam = self._rank(context, initial_candidates, evaluated)[: self.beam_width]

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

            beam = self._rank(context, expanded, evaluated)[: self.beam_width]

        finalists = [plan for plan in beam if plan["feasibility"]["feasible"]]
        diagnostics = {"scope": "EXPLORED_CANDIDATES",
                       "evaluated_candidate_count": len(evaluated)}
        if not finalists:
            diagnostics.update({
                "reason_code": "GATE_DEADLINE_MISSED",
                "best_infeasible_candidate": min(evaluated.values(), key=self._minutes_late),
            })
        return SearchOutcome(finalists, diagnostics)
