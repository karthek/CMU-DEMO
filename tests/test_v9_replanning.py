from dataclasses import replace
from datetime import timedelta
import unittest
from travel_agent.live.replanning import (Decision, PlanSnapshot, Reason, ReplanningEvaluator, ReplanningPolicy, TravelState)
from travel_agent.live.time import parse_instant


class V9ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.base = PlanSnapshot("s1", parse_instant("2026-09-11T19:00Z"), terminal="A", gate="A1",
            road_minutes=30, rideshare_pickup_minutes=5, rideshare_travel_minutes=30, security_minutes=10,
            selected_parking_available=True)
        self.evaluator = ReplanningEvaluator()

    def evaluate(self, current, **kwargs):
        return self.evaluator.evaluate(self.base, current, state=kwargs.pop("state", TravelState.PLANNING), **kwargs)

    def test_exact_deterioration_boundaries(self):
        for field, initial in (("road_minutes", 30), ("security_minutes", 10), ("rideshare_pickup_minutes", 5)):
            for delta, expected in ((-10, Decision.NO_CHANGE), (9, Decision.NO_CHANGE), (10, Decision.REPLAN)):
                if initial + delta < 0:
                    continue
                with self.subTest(field=field, delta=delta):
                    self.assertEqual(self.evaluate(replace(self.base, **{field: initial + delta})).decision, expected)

    def test_combined_rideshare_delta(self):
        current = replace(self.base, rideshare_pickup_minutes=9, rideshare_travel_minutes=36)
        self.assertIn(Reason.RIDESHARE_DETERIORATED, [e.reason for e in self.evaluate(current).evidence])

    def test_schedule_boundaries_both_directions(self):
        for minutes, expected in ((14, Decision.NO_CHANGE), (15, Decision.REPLAN), (-14, Decision.NO_CHANGE), (-15, Decision.REPLAN)):
            self.assertEqual(self.evaluate(replace(self.base, departure=self.base.departure + timedelta(minutes=minutes))).decision, expected)

    def test_enroute_delay_continue_not_turnaround(self):
        current = replace(self.base, departure=self.base.departure + timedelta(hours=1))
        result = self.evaluate(current, state=TravelState.EN_ROUTE_TO_AIRPORT)
        self.assertEqual(result.decision, Decision.INFORMATIONAL)
        self.assertTrue(result.continue_to_airport)
        result = self.evaluate(replace(current, road_minutes=40), state=TravelState.EN_ROUTE_TO_AIRPORT)
        self.assertTrue(result.replan_required)
        self.assertTrue(result.continue_to_airport)

    def test_terminal_gate_null_and_cancellation(self):
        self.assertEqual(self.evaluate(replace(self.base, terminal="B")).decision, Decision.REPLAN)
        self.assertEqual(self.evaluate(replace(self.base, gate="A2")).decision, Decision.INFORMATIONAL)
        self.assertEqual(self.evaluate(replace(self.base, terminal=None, gate=None)).decision, Decision.NO_CHANGE)
        result = self.evaluate(replace(self.base, cancelled=True))
        self.assertTrue(result.replan_required)
        self.assertFalse(result.reliable_plan_allowed)
        self.assertEqual(result.evidence[0].reason, Reason.FLIGHT_CANCELLED)

    def test_calendar_parking_not_prices(self):
        self.assertTrue(self.evaluate(replace(self.base, active_window_conflicts=frozenset({"event1"}))).replan_required)
        self.assertTrue(self.evaluate(replace(self.base, selected_parking_available=False)).replan_required)
        self.assertEqual(self.evaluate(replace(self.base, parking_price=100, rideshare_fare=1000)).decision, Decision.NO_CHANGE)

    def test_required_unavailable_blocks_and_recovers(self):
        missing = replace(self.base, road_minutes=None)
        result = self.evaluate(missing)
        self.assertEqual(result.decision, Decision.DATA_UNAVAILABLE)
        self.assertFalse(result.reliable_plan_allowed)
        self.assertIn("road_minutes", result.missing_required)
        recovered = self.evaluate(self.base, previously_unavailable=frozenset({"road_minutes"}))
        self.assertEqual(recovered.decision, Decision.REPLAN)
        self.assertEqual(recovered.evidence[0].reason, Reason.REQUIRED_DATA_RECOVERED)

    def test_plan_relative_comparison_and_statelessness(self):
        self.assertEqual(self.evaluate(replace(self.base, road_minutes=36)).decision, Decision.NO_CHANGE)
        later = replace(self.base, road_minutes=40)
        result = self.evaluate(later)
        self.assertEqual(result.decision, Decision.REPLAN)
        self.assertEqual(result, self.evaluate(later))
        self.assertEqual(vars(self.evaluator), {})

    def test_arrived_stops_road_and_departed_stops_monitor_decisions(self):
        current = replace(self.base, road_minutes=60)
        self.assertEqual(self.evaluate(current, state=TravelState.ARRIVED_AT_AIRPORT).decision, Decision.NO_CHANGE)
        self.assertFalse(self.evaluate(current, state=TravelState.FLIGHT_DEPARTED).replan_required)

    def test_custom_policy_invalid_inputs(self):
        self.assertTrue(self.evaluate(replace(self.base, road_minutes=39), policy=ReplanningPolicy(traffic_deterioration_minutes=9)).replan_required)
        self.assertEqual(ReplanningPolicy().same_terminal_gate_buffer_minutes, 5)
        for value in (-1, True, 1.5, "10", None):
            with self.assertRaises(ValueError):
                ReplanningPolicy(traffic_deterioration_minutes=value)
        with self.assertRaises(ValueError):
            self.evaluate(replace(self.base, segment_id="different"))
        with self.assertRaises(ValueError):
            replace(self.base, road_minutes=True)
