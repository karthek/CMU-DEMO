import copy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from test_itinerary_agent import snapshot
from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock
from travel_agent.itinerary.locking import MonitorLock
from travel_agent.itinerary.policy import ActivationPolicy


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = snapshot()
        self.itineraries = self.root / "itineraries.json"
        self.flights = self.root / "flights.json"
        self.flight_data = json.loads((Path(__file__).resolve().parents[1] / "fixtures/v8/flights.json").read_text())
        self.write()
        self.service = create_itinerary_service(database_path=self.root / "test.sqlite3", itinerary_path=self.itineraries,
                                                flight_path=self.flights, clock=FixedClock(datetime(2026, 9, 10, 19)))

    def write(self):
        self.itineraries.write_text(json.dumps(self.data), encoding="utf-8")
        self.flights.write_text(json.dumps(self.flight_data), encoding="utf-8")

    def tearDown(self):
        self.service.repository.close()
        self.temp.cleanup()


class ServiceTests(ServiceFixture):
    def test_shared_v7_handoff_and_durable_planning_before_lookup(self):
        original = self.service.travel_service.get_trip_context
        observed = []
        def lookup(*args):
            # A second connection cannot see an uncommitted PLANNING row.
            observer = sqlite3.connect(self.service.repository.path, timeout=0)
            try:
                observer.execute("PRAGMA foreign_keys=ON")
                rows = observer.execute("SELECT state FROM planning_attempts ORDER BY attempt_id").fetchall()
                observed.append(rows[-1][0])
            finally:
                observer.close()
            return original(*args)
        with patch.object(self.service.travel_service, "get_trip_context", side_effect=lookup), \
             patch.object(self.service.travel_service, "evaluate_trip_plans", wraps=self.service.travel_service.evaluate_trip_plans) as evaluate:
            auto = self.service.monitor_trips()
            manual = self.service.plan_booked_trip(selector={})
            self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(observed, ["PLANNING", "PLANNING"])
        self.assertEqual(auto["dispatches"][0]["attempt"]["planning_result"], manual["run"]["planning_result"])

    def test_manual_clock_once_and_departed_missing_excluded(self):
        clock = Mock()
        clock.now.return_value = datetime(2026, 9, 11, 19)
        self.service.clock = clock
        self.assertEqual(self.service.plan_booked_trip(selector={})["status"], "NOT_FOUND")
        clock.now.assert_called_once()
        self.data["records"] = []
        self.write()
        self.assertEqual(self.service.plan_booked_trip(selector={})["matches"], [])

    def test_final_persistence_failure_stops_later_dispatch(self):
        second = copy.deepcopy(self.data["records"][0])
        second["itinerary_id"] = "TRIP-002"
        self.data["records"].append(second)
        self.write()
        with patch.object(self.service.repository, "finish_attempt", side_effect=sqlite3.OperationalError), \
             patch.object(self.service.travel_service, "evaluate_trip_plans", wraps=self.service.travel_service.evaluate_trip_plans) as evaluate:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.monitor_trips()
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(self.service.repository.automatic_state("fixture/TRIP-001/OUT")["state"], "PLANNING")

    def test_auto_plan_and_one_clock_read(self):
        clock = Mock()
        clock.now.return_value = datetime(2026, 9, 10, 19)
        self.service.clock = clock
        result = self.service.monitor_trips()
        clock.now.assert_called_once_with()
        dispatch = result["dispatches"][0]
        self.assertEqual((dispatch["decision"], dispatch["automatic_state_after"]), ("PLAN", "COMPLETED"))
        self.assertEqual(dispatch["attempt"]["planning_result"]["selected_plan"]["score"], 77.2)
        self.assertEqual(dispatch["attempt"]["trigger"], "AUTOMATIC_LEAD_TIME")

    def test_manual_early_and_independent(self):
        self.service.clock = FixedClock(datetime(2026, 9, 6, 19))
        result = self.service.plan_booked_trip(selector={"destination": "PHL"})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["run"]["activation_result"]["status"], "NOT_YET_ELIGIBLE")
        self.assertEqual(result["run"]["trigger"], "USER_REQUEST")
        self.assertEqual(self.service.repository.connection.execute("SELECT count(*) FROM automatic_planning_state").fetchone()[0], 0)
        self.service.clock = FixedClock(datetime(2026, 9, 10, 19))
        self.assertEqual(self.service.monitor_trips()["dispatches"][0]["decision"], "PLAN")
        state = dict(self.service.repository.automatic_state("fixture/TRIP-001/OUT"))
        self.assertEqual(self.service.plan_booked_trip(selector={})["status"], "COMPLETED")
        self.assertEqual(self.service.repository.automatic_state("fixture/TRIP-001/OUT"), state)

    def test_resolution_zero_multiple_exclusions(self):
        self.assertEqual(self.service.plan_booked_trip(selector={"destination": "JFK"})["status"], "NOT_FOUND")
        second = copy.deepcopy(self.data["records"][0])
        second["itinerary_id"] = "TRIP-002"
        self.data["records"].append(second)
        self.write()
        result = self.service.plan_booked_trip(selector={})
        self.assertEqual(result["status"], "NEEDS_SELECTION")
        self.assertEqual(len(result["matches"]), 2)
        for record in self.data["records"]:
            record["segments"][0]["booking_status"] = "CANCELLED"
        self.write()
        self.assertEqual(self.service.plan_booked_trip(selector={})["status"], "NOT_FOUND")
        with self.assertRaises(ValueError):
            self.service.plan_booked_trip(selector={"segment_id": "OUT"})

    def test_failed_refresh_blocks_both_paths(self):
        self.service.monitor_trips()
        self.data["records"].append(None)
        self.write()
        with patch.object(self.service.travel_service, "get_trip_context") as lookup:
            self.assertEqual(self.service.monitor_trips()["dispatches"], [])
            self.assertEqual(self.service.plan_booked_trip(selector={})["status"], "REFRESH_BLOCKED")
            lookup.assert_not_called()

    def test_multiple_independent_and_order(self):
        second = copy.deepcopy(self.data["records"][0])
        second["itinerary_id"] = "AAA"
        second["segments"][0]["flight_number"] = "AA123"
        self.data["records"].append(second)
        self.write()
        result = self.service.monitor_trips()
        self.assertEqual([d["itinerary_id"] for d in result["dispatches"]], ["fixture/AAA", "fixture/TRIP-001"])
        self.assertEqual([d["automatic_state_after"] for d in result["dispatches"]], ["FAILED", "COMPLETED"])

    def test_busy_and_no_refresh(self):
        with MonitorLock(self.service.repository.path), patch.object(self.service.agent, "refresh") as refresh:
            result = self.service.monitor_trips()
            self.assertEqual(result["diagnostics"][0]["code"], "MONITOR_BUSY")
            refresh.assert_not_called()

    def test_persistence_failure_prevents_planning(self):
        with patch.object(self.service.repository, "start_attempt", side_effect=sqlite3.OperationalError), patch.object(self.service.travel_service, "get_trip_context") as lookup:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.monitor_trips()
            lookup.assert_not_called()

    def test_manual_v7_no_feasible_and_forged_fields(self):
        candidates = [{"label": "late", "leave_time": "2026-09-11T20:00", "summary": "late",
                       "score": 999, "feasibility": {"feasible": True}}]
        result = self.service.plan_booked_trip(selector={}, candidates=candidates)
        self.assertEqual(result["run"]["planning_result"]["status"], "NO_FEASIBLE_PLAN")
        self.assertIsNone(result["run"]["planning_result"]["selected_plan"])
        self.assertEqual(result["status"], "COMPLETED")

    def test_roundtrip_outbound_only(self):
        segment = copy.deepcopy(self.data["records"][0]["segments"][0])
        segment.update(segment_id="RETURN", flight_number="DL1426", origin="PHL", destination="ATL",
                       departure_date="2026-09-14", scheduled_departure="2026-09-14T18:00")
        self.data["records"][0]["segments"].append(segment)
        self.write()
        result = self.service.monitor_trips()
        self.assertEqual([a["status"] for a in result["activation_results"]], ["ELIGIBLE", "NOT_YET_ELIGIBLE"])
        self.assertEqual(len(result["activated_segments"]), 1)
