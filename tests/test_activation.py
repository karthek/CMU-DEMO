from dataclasses import replace
from datetime import datetime
import unittest
from test_itinerary_agent import Source
from travel_agent.agents.itinerary_agent import ItineraryAgent
from travel_agent.itinerary.activation import ActivationEvaluator
from travel_agent.itinerary.policy import ActivationPolicy, DEFAULT_ACTIVATION_POLICY


class ActivationTests(unittest.TestCase):
    def test_policy_frozen_shared_default_and_no_clock(self):
        import inspect
        from dataclasses import FrozenInstanceError
        from unittest.mock import patch
        self.assertIs(inspect.signature(ActivationEvaluator.evaluate).parameters["policy"].default, DEFAULT_ACTIVATION_POLICY)
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_ACTIVATION_POLICY.planning_lead_time_minutes = 60
        with patch("travel_agent.itinerary.clock.SystemClock.now", side_effect=AssertionError("hidden clock")):
            now = datetime(2026, 9, 10, 19)
            self.assertEqual(self.evaluator.evaluate(self.segment, now), self.evaluator.evaluate(self.segment, now))

    def setUp(self):
        self.segment = ItineraryAgent(Source()).refresh().itineraries[0].segments[0]
        self.evaluator = ActivationEvaluator()

    def test_boundaries(self):
        cases = [("2026-09-10T18:59", "NOT_YET_ELIGIBLE"), ("2026-09-10T19:00", "ELIGIBLE"),
                 ("2026-09-10T19:01", "ELIGIBLE"), ("2026-09-11T18:59", "ELIGIBLE"),
                 ("2026-09-11T19:00", "DEPARTED"), ("2026-09-11T19:01", "DEPARTED")]
        for time, status in cases:
            result = self.evaluator.evaluate(self.segment, datetime.fromisoformat(time))
            self.assertEqual(result.status, status)
            self.assertEqual(result.activation_time, datetime(2026, 9, 10, 19))
            self.assertEqual(result.eligible, status == "ELIGIBLE")
            self.assertEqual(result.to_dict()["evidence"]["rule"], "AUTOMATIC_PLANNING_WINDOW_V1")

    def test_policy(self):
        self.assertEqual(DEFAULT_ACTIVATION_POLICY.planning_lead_time_minutes, 1440)
        for value in (-1, True, False, 2.5, "24", None):
            with self.assertRaises(ValueError):
                ActivationPolicy(value)
        self.assertFalse(self.evaluator.evaluate(self.segment, datetime(2026, 9, 10, 19), ActivationPolicy(60)).eligible)
        self.assertFalse(self.evaluator.evaluate(self.segment, datetime(2026, 9, 11, 19), ActivationPolicy(0)).eligible)

    def test_cancelled_invalid_and_fractional_evidence(self):
        now = datetime(2026, 9, 10, 19, 0, 30)
        self.assertEqual(self.evaluator.evaluate(replace(self.segment, booking_status="CANCELLED"), now).status, "CANCELLED")
        self.assertEqual(self.evaluator.evaluate(None, now).status, "INVALID_SEGMENT")
        self.assertEqual(self.evaluator.evaluate(self.segment, now).time_until_departure_minutes, 1439.5)
