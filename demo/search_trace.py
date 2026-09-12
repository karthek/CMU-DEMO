"""Demo-only observation of the frozen production _rank boundary.

Private coupling: search_outcome calls _rank once per stage and immediately takes
ranked[:beam_width]. Refinement inputs contain retained parents followed by their
children. No search, feasibility, or scoring implementation is copied here.
Snapshots are detached; production never reads them. Recheck parity if the private
boundary changes. Parent links are reported only when observed history, label and
time shift identify exactly one parent from the preceding beam.
"""
from copy import deepcopy
from datetime import datetime

from travel_agent.planning.beam_search import BeamSearchPlanner


class TracedBeamSearchPlanner(BeamSearchPlanner):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stages = []

    def search_outcome(self, context, initial_candidates):
        self.stages = []
        return super().search_outcome(context, initial_candidates)

    def _rank(self, context, plans, evaluated=None):
        entered = deepcopy(plans)
        ranked = super()._rank(context, plans, evaluated)
        # Only detached data crosses into the observer; its return is ignored.
        self._record(entered, deepcopy(ranked), self.beam_width)
        return ranked

    def _record(self, entered, ranked, width):
        self.stages.append({"stage": len(self.stages) + 1, "entered": entered,
                            "ranked": ranked, "retained": deepcopy(ranked[:width])})


def stage_rows(stage, previous_beam=()):
    """Annotate observed entries, including duplicates never independently scored."""
    key = lambda plan: datetime.fromisoformat(plan["leave_time"])
    evaluations = {key(plan): plan for plan in stage["ranked"]}
    retained = {key(plan) for plan in stage["retained"]}
    seen = set()
    rows = []
    for plan in stage["entered"]:
        departure = key(plan)
        duplicate = departure in seen
        seen.add(departure)
        evaluation = evaluations[departure]
        parents = []
        for parent in previous_beam:
            shift = (departure - key(parent)).total_seconds() / 60
            suffix = "earlier" if shift == -10 else "later" if shift == 10 else None
            if (suffix and plan["label"] == f"{parent['label']} / {suffix}"
                and plan.get("history", []) == parent.get("history", []) + [
                    f"Depth {stage['stage'] - 1}: shifted 10 min {suffix}"]):
                parents.append(parent["leave_time"])
        rows.append({"departure": plan["leave_time"],
                     "parent": parents[0] if len(parents) == 1 else None,
                     "feasible": evaluation["feasibility"]["feasible"],
                     "score": evaluation.get("score"),
                     "duplicate": duplicate,
                     "status": "duplicate/deduplicated" if duplicate else
                               "retained" if departure in retained else "outside beam width / lower ranked"})
    return rows


def render_trace(stages, width, depth, finalists):
    lines = ["=== BEAM SEARCH TRACE ===", f"Width: {width}", f"Depth: {depth}",
             "Host proposes. Production Beam Search / feasibility / Critic evaluate.",
             "Depth counts initial selection plus refinement stages; feasibility is separate from beam retention."]
    previous = []
    for stage in stages:
        number = stage["stage"]
        title = "Initial Selection" if number == 1 else f"Refinement {number - 1}"
        lines += [f"Stage {number} - {title}", "Entered (generated links shown where observed):"]
        for row in stage_rows(stage, previous):
            score = "not scored" if row["score"] is None else f"{row['score']:.2f}"
            link = f"{row['parent']} -> " if row["parent"] else ""
            note = "; evaluation of first occurrence, duplicate not rescored" if row["duplicate"] else ""
            feasible = "feasible" if row["feasible"] else "INFEASIBLE"
            lines.append(f"  {link}{row['departure']} | {feasible} | score {score} | {row['status']}{note}")
        lines.append("SEARCH BEAM: " + ", ".join(
            f"{p['leave_time']} ({'feasible' if p['feasibility']['feasible'] else 'INFEASIBLE; search only'})"
            for p in stage["retained"]))
        previous = stage["retained"]
    lines.append("RECOMMENDABLE FINALISTS (feasible only): " +
                 (", ".join(p["leave_time"] for p in finalists) or "none"))
    return "\n".join(lines)
