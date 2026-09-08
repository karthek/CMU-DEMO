import copy
from datetime import datetime
import unittest

from travel_agent.composition import create_simulated_coordinator
from travel_agent.planning.baseline import generate_baseline_candidates
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.planning.critic import PlanCritic


def plan(time, label="Seed"):
    return {"label": label, "leave_time": f"2026-09-11T{time}",
            "summary": label, "history": [label]}


class Scores:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def score(self, context, candidate):
        time = datetime.fromisoformat(candidate["leave_time"]).strftime("%H:%M")
        self.calls.append(time)
        return self.values.get(time, 0)


class BeamSearchTests(unittest.TestCase):
    def test_best_parent_survives_worse_children(self):
        seed = plan("16:00")
        for depth in (1, 2, 3, 5):
            result = BeamSearchPlanner(1, depth, Scores({"16:00": 100})).search(None, [seed])
            self.assertEqual(result[0]["leave_time"], seed["leave_time"])
            self.assertEqual(result[0]["score"], 100)
            self.assertEqual(result[0]["history"], ["Seed"])

    def test_better_child_replaces_parent(self):
        result = BeamSearchPlanner(1, 2, Scores({"16:00": 5, "16:10": 10})).search(None, [plan("16:00")])
        self.assertEqual(result[0]["leave_time"], "2026-09-11T16:10")
        self.assertEqual(result[0]["history"], ["Seed", "Depth 1: shifted 10 min later"])

    def test_equivalent_timestamps_deduplicate_before_scoring(self):
        critic = Scores({"16:00": 1})
        result = BeamSearchPlanner(2, 1, critic).search(None, [plan("16:00", "First"), plan("16:00:00", "Second")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["history"], ["First"])
        self.assertEqual(critic.calls, ["16:00"])

    def test_finalists_distinct_and_width_enforced(self):
        for width in (1, 2, 4):
            result = BeamSearchPlanner(width, 4, Scores({})).search(None, [plan("16:00"), plan("16:10"), plan("16:00")])
            times = [datetime.fromisoformat(p["leave_time"]) for p in result]
            self.assertEqual(len(times), len(set(times)))
            self.assertLessEqual(len(result), width)

    def test_ties_preserve_input_order_and_parents(self):
        seeds = [plan("16:00", "First"), plan("16:10", "Second")]
        result = BeamSearchPlanner(2, 3, Scores({})).search(None, seeds)
        self.assertEqual([p["history"] for p in result], [["First"], ["Second"]])

    def test_duplicate_children_keep_first_real_path(self):
        # 16:10 is reached by the first parent's later child and second's earlier child.
        result = BeamSearchPlanner(2, 2, Scores({"16:10": 100})).search(
            None, [plan("16:00", "First"), plan("16:20", "Second")])
        self.assertEqual(result[0]["history"], ["First", "Depth 1: shifted 10 min later"])

    def test_repeated_runs_identical_and_inputs_unchanged(self):
        seeds = [plan("16:00"), plan("16:20")]
        original = copy.deepcopy(seeds)
        planner = BeamSearchPlanner(2, 3, Scores({"16:10": 10}))
        first = planner.search(None, seeds)
        for _ in range(4):
            self.assertEqual(planner.search(None, seeds), first)
        self.assertEqual(seeds, original)

    def test_depth_counts_initial_ranking_plus_refinement_rounds(self):
        for depth, expected in [(1, "16:00"), (2, "16:10"), (3, "16:20")]:
            scores = Scores({"16:00": 1, "16:10": 2, "16:20": 3})
            result = BeamSearchPlanner(1, depth, scores).search(None, [plan("16:00")])
            self.assertEqual(result[0]["leave_time"], f"2026-09-11T{expected}")
            self.assertEqual(len(scores.calls), 1 + 3 * (depth - 1))

    def test_empty_candidates_rejected(self):
        with self.assertRaisesRegex(ValueError, "initial_candidates"):
            BeamSearchPlanner().search(None, [])

    def test_invalid_width_rejected(self):
        for value in (0, -1, True, 1.5, "2", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "beam_width"):
                BeamSearchPlanner(beam_width=value)

    def test_invalid_depth_rejected(self):
        for value in (0, -1, False, 2.5, "3", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "depth"):
                BeamSearchPlanner(depth=value)

    def test_existing_critic_scores_verified_winner(self):
        context = create_simulated_coordinator().get_trip_context("DL1425", "2026-09-11")
        self.assertEqual(PlanCritic().score(context, plan("16:25")), 77.2)
        result = BeamSearchPlanner().search(context, generate_baseline_candidates(context))
        self.assertEqual([(p["leave_time"], p["score"]) for p in result],
                         [("2026-09-11T16:25", 77.2), ("2026-09-11T16:15", 76.4)])
