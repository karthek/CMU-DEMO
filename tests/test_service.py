import copy
import json
import unittest
from unittest.mock import patch

from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.planner = BeamSearchPlanner(beam_width=2, depth=3)
        self.service = TravelService(TravelCoordinator(
            FakeFlightTool(), FakeCalendarTool(), self.planner,
        ))
        self.context = self.service.get_trip_context("DL1425", "2026-09-11")
        self.candidates = [{
            "label": "Host buffer",
            "leave_time": "2026-09-11T16:20",
            "summary": "A host-provided option",
            "history": ["Host suggestion"],
        }]

    def test_context_and_result_round_trip_through_json(self):
        context = json.loads(json.dumps(self.context))
        result = self.service.evaluate_trip_plans(context)
        self.assertEqual(json.loads(json.dumps(result)), result)
        self.assertEqual(context["flight"]["boarding_time"], "2026-09-11T18:30")
        self.assertEqual(len(context["calendar_events"]), 2)

    def test_omitted_and_none_candidates_use_baseline(self):
        result = self.service.evaluate_trip_plans(self.context)
        self.assertEqual(result, self.service.evaluate_trip_plans(self.context, None))
        self.assertEqual(result["mode"], "deterministic")
        self.assertEqual(result["selected_plan"]["leave_time"], "2026-09-11T16:25")
        self.assertEqual(result["selected_plan"]["score"], 77.2)

    def test_host_candidates_use_same_planner_without_baseline(self):
        with patch("travel_agent.coordinator.generate_baseline_candidates") as baseline:
            result = self.service.evaluate_trip_plans(self.context, self.candidates)
        baseline.assert_not_called()
        expected = self.planner.search(
            self.service.coordinator.get_trip_context("DL1425", "2026-09-11"),
            self.candidates,
        )
        self.assertEqual(result["finalists"], expected)
        self.assertEqual(result["mode"], "host-assisted")
        self.assertEqual(result["selected_plan"], expected[0])

    def test_host_score_is_ignored_and_inputs_not_mutated(self):
        expected = self.service.evaluate_trip_plans(self.context, self.candidates)
        self.candidates[0]["score"] = 1000000
        original = copy.deepcopy((self.context, self.candidates))
        actual = self.service.evaluate_trip_plans(self.context, self.candidates)
        self.assertEqual(actual, expected)
        self.assertEqual((self.context, self.candidates), original)

    def test_history_is_optional(self):
        del self.candidates[0]["history"]
        result = self.service.evaluate_trip_plans(self.context, self.candidates)
        self.assertEqual(len(result["selected_plan"]["history"]), 0)

    def test_bad_candidate_inputs_fail_before_search(self):
        invalid = [[], {}, "plans", [None]]
        for field, value in [
            ("label", ""), ("summary", None), ("leave_time", "bad"),
            ("leave_time", "2026-09-11"),
            ("leave_time", "2026-09-11T16:20+00:00"),
            ("history", "not a list"), ("history", [42]),
        ]:
            plan = dict(self.candidates[0])
            plan[field] = value
            invalid.append([plan])
        with patch.object(self.planner, "search_outcome") as search:
            for candidates in invalid:
                with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                    self.service.evaluate_trip_plans(self.context, candidates)
            search.assert_not_called()

    def test_bad_context_inputs_fail_before_search(self):
        invalid = [None, {}, {**self.context, "unknown": 1}]
        for field, value in [
            ("airport_travel_minutes", -1), ("security_minutes", "20"),
            ("gate_walk_minutes", True), ("calendar_events", None),
        ]:
            invalid.append({**self.context, field: value})
        for field, value in [
            ("departure_time", "bad"),
            ("boarding_time", "2026-09-11T20:00"),
        ]:
            context = copy.deepcopy(self.context)
            context["flight"][field] = value
            invalid.append(context)
        context = copy.deepcopy(self.context)
        context["calendar_events"][0]["end"] = "2026-09-11T13:00"
        invalid.append(context)
        with patch.object(self.planner, "search_outcome") as search:
            for context in invalid:
                with self.subTest(context=context), self.assertRaises(ValueError):
                    self.service.evaluate_trip_plans(context)
            search.assert_not_called()

    def test_bad_request_is_rejected(self):
        for flight, date in [("", "2026-09-11"), (None, "2026-09-11"),
                             ("DL1425", "tomorrow"), ("DL1425", "20260911")]:
            with self.subTest(flight=flight, date=date), self.assertRaises(ValueError):
                self.service.get_trip_context(flight, date)
