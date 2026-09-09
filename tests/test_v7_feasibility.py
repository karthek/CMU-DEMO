"""V7 hard feasibility, bounded recovery, explanations and trust contracts."""
import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import inspect
import unittest
from unittest.mock import patch

from travel_agent.composition import create_simulated_coordinator
from travel_agent.models import CalendarEvent
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.planning.critic import PlanCritic
from travel_agent.planning.feasibility import evaluate_feasibility
from travel_agent.planning.models import ScoreEvaluation, SearchOutcome
from travel_agent.planning.policy import DEFAULT_PLANNING_POLICY, PlanningPolicy
from travel_agent.service import TravelService


def plan(time):
    return {"label": "Proposal", "leave_time": f"2026-09-11T{time}",
            "summary": "Proposed departure", "history": ["Seed"]}


class V7Tests(unittest.TestCase):
    def setUp(self):
        self.coordinator = create_simulated_coordinator()
        self.context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.service = TravelService(self.coordinator)
        self.wire = self.service.get_trip_context("DL1425", "2026-09-11")

    def test_search_wrapper_delegates_to_authoritative_outcome(self):
        planner = BeamSearchPlanner()
        candidates = [plan("16:20")]
        for finalists in ([], [{"marker": "authoritative finalists"}]):
            outcome = SearchOutcome(finalists, {"marker": "diagnostics"})
            with patch.object(planner, "search_outcome", return_value=outcome) as search:
                self.assertIs(planner.search(self.context, candidates), finalists)
                search.assert_called_once_with(self.context, candidates)

    def test_score_wrapper_delegates_to_authoritative_evaluation(self):
        critic = PlanCritic()
        candidate = plan("16:20")
        evaluation = ScoreEvaluation(12.34567, {"marker": "authoritative breakdown"}, [])
        with patch.object(critic, "evaluate", return_value=evaluation) as evaluate:
            self.assertEqual(critic.score(self.context, candidate), evaluation.score)
            evaluate.assert_called_once_with(self.context, candidate)

    def test_gate_boundaries(self):
        for time, feasible, late in [("16:00", True, None), ("17:25", True, None),
                                     ("17:26", False, 1.0), ("17:41", False, 16.0)]:
            with self.subTest(time=time):
                result = evaluate_feasibility(self.context, plan(time)).to_dict()
                self.assertEqual(result["feasible"], feasible)
                self.assertEqual(result["gate_deadline"], "2026-09-11T18:45:00")
                arrival = datetime.fromisoformat(plan(time)["leave_time"]) + timedelta(minutes=80)
                self.assertEqual(result["gate_arrival_time"], arrival.isoformat())
                self.assertEqual(result["deadline_source"], "configured_policy")
                self.assertEqual(result["hard_constraint_failures"], [] if feasible else
                                 [{"code": "GATE_DEADLINE_MISSED", "minutes_late": late}])

    def test_boarding_is_soft(self):
        self.context.flight.boarding_time = "2026-09-11T18:25"
        candidate = plan("17:15")  # At gate 18:35, after boarding, before deadline.
        self.assertTrue(evaluate_feasibility(self.context, candidate).feasible)
        evaluation = PlanCritic().evaluate(self.context, candidate)
        self.assertEqual(evaluation.score_breakdown["components"][1]["value"], -100)

    def test_departure_deadlines_and_delay_not_added(self):
        for departure, deadline in [("2026-09-11T20:00", "2026-09-11T19:45:00"),
                                    ("2026-09-12T00:05", "2026-09-11T23:50:00")]:
            self.context.flight.departure_time = departure
            self.context.flight.delay_minutes = 90
            self.assertEqual(evaluate_feasibility(self.context, plan("17:00")).gate_deadline, deadline)

    def test_policy_override_and_zero(self):
        for buffer, deadline, feasible in [(30, "18:30:00", False), (0, "19:00:00", True)]:
            policy = PlanningPolicy(buffer)
            result = evaluate_feasibility(self.context, plan("17:20"), policy)
            self.assertEqual(result.gate_deadline, "2026-09-11T" + deadline)
            self.assertEqual(result.feasible, feasible)
            self.assertIs(create_simulated_coordinator(policy=policy).planner.policy, policy)

    def test_policy_authoritative_default_and_frozen(self):
        self.assertEqual(DEFAULT_PLANNING_POLICY.gate_close_buffer_minutes, 15)
        self.assertIs(BeamSearchPlanner().policy, DEFAULT_PLANNING_POLICY)
        self.assertIs(inspect.signature(evaluate_feasibility).parameters["policy"].default,
                      DEFAULT_PLANNING_POLICY)
        self.assertIs(self.coordinator.planner.policy, DEFAULT_PLANNING_POLICY)
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_PLANNING_POLICY.gate_close_buffer_minutes = 1

    def test_invalid_policy(self):
        for value in (-1, True, False, 1.5, "15", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PlanningPolicy(value)

    def test_lower_scoring_feasible_beats_attractive_infeasible(self):
        self.context.calendar_events = []
        feasible, infeasible = plan("00:00"), plan("17:26")
        feasible["leave_time"] = "2026-09-10T00:00"
        self.assertLess(PlanCritic().score(self.context, feasible), PlanCritic().score(self.context, infeasible))
        result = BeamSearchPlanner(1, 1).search(self.context, [infeasible, feasible])
        self.assertEqual(result[0]["leave_time"], feasible["leave_time"])
        self.assertTrue(result[0]["feasibility"]["feasible"])

    def test_unused_beam_slot_retains_recovery_without_score(self):
        planner = BeamSearchPlanner()
        ranked = planner._rank(self.context, [plan("17:40"), plan("16:00"), plan("17:30")])
        self.assertEqual([p["leave_time"][-5:] for p in ranked[:2]], ["16:00", "17:30"])
        self.assertNotIn("score", ranked[1])
        self.assertNotIn("score_breakdown", ranked[1])

    def test_recovery_after_two_earlier_refinements(self):
        # Gate deadline 18:15, so latest feasible leave is 16:55.
        policy = PlanningPolicy(45)
        seed = plan("17:10")
        for depth in (1, 2):
            self.assertEqual(BeamSearchPlanner(depth=depth, policy=policy).search(self.context, [seed]), [])
        result = BeamSearchPlanner(policy=policy).search(self.context, [seed])
        self.assertEqual(result[0]["leave_time"], "2026-09-11T16:50")
        self.assertEqual(result[0]["history"], ["Seed", "Depth 1: shifted 10 min earlier",
                                               "Depth 2: shifted 10 min earlier"])
        self.assertTrue(all(p["feasibility"]["feasible"] for p in result))

    def test_recovery_ties_and_dedup_preserve_first_encounter(self):
        first, second = plan("18:00"), plan("18:00:00")
        second["label"] = "Duplicate"
        ranked = BeamSearchPlanner()._rank(self.context, [first, second])
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["label"], "Proposal")

    def test_infeasible_not_scored(self):
        class RejectCritic:
            def evaluate(self, context, candidate):
                raise AssertionError("Infeasible candidate reached Critic")
        self.assertEqual(BeamSearchPlanner(critic=RejectCritic()).search(self.context, [plan("20:00")]), [])

    def test_all_infeasible_core_and_service(self):
        candidates = [plan("20:00")]
        outcome = self.coordinator.planner.search_outcome(self.context, candidates)
        result = self.service.evaluate_trip_plans(self.wire, candidates)
        self.assertEqual(outcome.finalists, [])
        self.assertEqual(result["status"], "NO_FEASIBLE_PLAN")
        self.assertIsNone(result["selected_plan"])
        self.assertEqual(result["finalists"], [])
        self.assertEqual(result["recommendation"], "No feasible plan was found within the explored candidates.")
        self.assertEqual(result["diagnostics"], outcome.diagnostics)
        self.assertEqual(outcome.diagnostics["scope"], "EXPLORED_CANDIDATES")
        self.assertEqual(outcome.diagnostics["evaluated_candidate_count"], 4)
        best = outcome.diagnostics["best_infeasible_candidate"]
        self.assertEqual(best["leave_time"], "2026-09-11T19:40")
        self.assertEqual(best["feasibility"]["hard_constraint_failures"][0]["minutes_late"], 135)
        self.assertNotIn("score", best)

    def test_only_feasible_finalists_and_selection(self):
        result = self.service.evaluate_trip_plans(self.wire, [plan("17:20"), plan("20:00")])
        self.assertEqual(result["status"], "PLAN_FOUND")
        self.assertTrue(result["selected_plan"]["feasibility"]["feasible"])
        self.assertTrue(all(p["feasibility"]["feasible"] for p in result["finalists"]))

    def test_defaults_width_and_depth(self):
        self.assertEqual((self.coordinator.planner.beam_width, self.coordinator.planner.depth), (2, 3))

    def test_score_reconciles_and_compatibility_method(self):
        critic = PlanCritic()
        for minute in range(0, 24 * 60, 7):
            candidate = plan(f"{minute // 60:02}:{minute % 60:02}:13")
            evaluation = critic.evaluate(self.context, candidate)
            self.assertEqual(round(sum(c["value"] for c in evaluation.score_breakdown["components"]), 2),
                             evaluation.score)
            self.assertEqual(critic.score(self.context, candidate), evaluation.score)
            self.assertEqual(evaluation.score_breakdown["components"][0], {"code": "BASE_SCORE", "value": 100.0})

    def test_boarding_penalty_branches(self):
        self.context.calendar_events = []
        boarding = datetime.fromisoformat(self.context.flight.boarding_time)
        for margin, penalty in [(-1, -100), (0, -35), (19, -35), (20, -10), (44, -10), (45, 0)]:
            candidate = plan("16:00")
            candidate["leave_time"] = (boarding - timedelta(minutes=80 + margin)).isoformat()
            evaluation = PlanCritic().evaluate(self.context, candidate)
            self.assertEqual(evaluation.score_breakdown["components"][1]["value"], penalty)

    def test_early_departure_unrounded(self):
        evaluation = PlanCritic().evaluate(self.context, plan("16:25:13"))
        value = evaluation.score_breakdown["components"][2]["value"]
        self.assertAlmostEqual(value, -2.7826666666666666)
        self.assertNotEqual(value, round(value, 2))

    def test_calendar_normal_high_and_double_boundary(self):
        for priority, leave, penalties in [("normal", "16:30", [-25]), ("high", "16:40", [-60]),
                                            ("high", "16:20", [-20]), ("high", "16:30", [-60, -20])]:
            with self.subTest(priority=priority, leave=leave):
                self.context.calendar_events = [CalendarEvent("Leadership Meeting", "2026-09-11T16:30",
                                                              "2026-09-11T17:00", priority)]
                evaluation = PlanCritic().evaluate(self.context, plan(leave))
                self.assertEqual([c["penalty"] for c in evaluation.calendar_conflicts], penalties)
                for conflict in evaluation.calendar_conflicts:
                    self.assertEqual(conflict["event_index"], 0)
                    self.assertEqual(conflict["title"], "Leadership Meeting")
                    self.assertEqual(conflict["start"], "2026-09-11T16:30:00")
                    self.assertEqual(conflict["end"], "2026-09-11T17:00:00")
                    self.assertEqual(conflict["priority"], priority)
                    component = evaluation.score_breakdown["components"][conflict["score_component_index"]]
                    self.assertEqual(component["value"], conflict["penalty"])
                base = sum(c["value"] for c in evaluation.score_breakdown["components"][:3])
                self.assertEqual(evaluation.score, round(base + sum(penalties), 2))
                expected = (["DEPARTURE_DURING_EVENT"] if penalties[0] != -20 else [])
                if -20 in penalties:
                    expected.append("DEPARTURE_BEFORE_OR_AT_EVENT_START")
                self.assertEqual([c["conflict_type"] for c in evaluation.calendar_conflicts], expected)

    def test_forged_host_fields_ignored_and_inputs_unchanged(self):
        for time in ("16:20", "20:00"):
            clean = [plan(time)]
            forged = copy.deepcopy(clean)
            forged[0].update(score=999999, feasible=True, feasibility={"feasible": True},
                             explanation="Trusted", score_breakdown={"components": []},
                             calendar_conflicts=[], gate_arrival_time="2026-09-11T16:00")
            before = copy.deepcopy((self.wire, forged))
            result = self.service.evaluate_trip_plans(self.wire, forged)
            self.assertEqual(result, self.service.evaluate_trip_plans(self.wire, clean))
            self.assertEqual((self.wire, forged), before)
            self.assertEqual(result, self.service.evaluate_trip_plans(self.wire, forged))

    def test_calendar_read_only_during_evaluation(self):
        before = copy.deepcopy(self.context)
        self.coordinator.evaluate_trip_plans(self.context, [plan("16:30")])
        self.assertEqual(self.context, before)
        from travel_agent.agents.calendar_agent import CalendarAgent
        from travel_agent.tools.calendar_tool import FakeCalendarTool
        self.assertEqual({name for name in dir(FakeCalendarTool) if not name.startswith("_")}, {"get_events"})
        self.assertFalse(any(word in name for name in dir(CalendarAgent)
                             for word in ("write", "reschedule", "cancel", "move", "update")))

    def test_service_signatures_unchanged(self):
        self.assertEqual(list(inspect.signature(TravelService.get_trip_context).parameters),
                         ["self", "flight_number", "date"])
        self.assertEqual(list(inspect.signature(TravelService.evaluate_trip_plans).parameters),
                         ["self", "context", "candidates"])

    def test_both_baseline_results_and_gate_times(self):
        host = [plan(time) for time in ("15:50", "16:20", "17:00")]
        for candidates, expected in [(None, [("16:25", 77.2, "17:45"), ("16:15", 76.4, "17:35")]),
                                     (host, [("16:20", 76.8, "17:40"), ("16:10", 76.0, "17:30")])]:
            result = self.service.evaluate_trip_plans(self.wire, candidates)
            self.assertEqual(result["status"], "PLAN_FOUND")
            self.assertEqual(len(result["finalists"]), 2)
            for candidate, (leave, score, arrival) in zip(result["finalists"], expected):
                self.assertEqual(candidate["leave_time"], "2026-09-11T" + leave)
                self.assertEqual(candidate["score"], score)
                self.assertEqual(candidate["feasibility"]["gate_arrival_time"], "2026-09-11T" + arrival + ":00")
                self.assertEqual(candidate["feasibility"]["gate_deadline"], "2026-09-11T18:45:00")
                self.assertTrue(candidate["feasibility"]["feasible"])
