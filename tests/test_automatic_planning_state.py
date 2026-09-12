from datetime import datetime
from unittest.mock import patch
from test_itinerary_service import ServiceFixture
from travel_agent.itinerary.clock import FixedClock
from travel_agent.itinerary.models import PlanningProvenance
from travel_agent.itinerary.repository import ItineraryRepository
from travel_agent.planning.policy import PlanningPolicy


class AutomaticStateTests(ServiceFixture):
    def test_actual_process_exit_leaves_recoverable_attempt(self):
        import subprocess
        import sys
        script = """
import os, sys
from datetime import datetime
from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock
from travel_agent.itinerary.locking import MonitorLock
from travel_agent.itinerary.models import PlanningProvenance
s = create_itinerary_service(database_path=sys.argv[1], itinerary_path=sys.argv[2], flight_path=sys.argv[3],
                             clock=FixedClock(datetime(2026,9,10,19)))
with MonitorLock(s.repository.path):
    now = s.clock.now()
    s.repository.apply_snapshot(s.agent.refresh(), as_of=now)
    segment = s.repository.list_current_segments()[0]
    s.repository.initialize_automatic_state(segment.segment_id, as_of=now)
    activation = s.evaluator.evaluate(segment, now)
    provenance = PlanningProvenance('AUTOMATIC','AUTOMATIC_LEAD_TIME',now,segment.itinerary_id,segment.segment_id,1)
    s.repository.start_attempt(segment, activation, provenance)
    os._exit(0)
"""
        process = subprocess.run([sys.executable, "-B", "-c", script, str(self.service.repository.path),
                                  str(self.itineraries), str(self.flights)], capture_output=True, timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(self.service.repository.automatic_state("fixture/TRIP-001/OUT")["state"], "PLANNING")
        result = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual((result["decision"], result["attempt"]["attempt_number"]), ("RETRY", 2))
        self.assertEqual(self.service.repository.get_attempt(1).error["code"], "INTERRUPTED_EXECUTION")

    def test_failed_cancelled_missing_and_outside_window_no_retry(self):
        from unittest.mock import patch
        with patch.object(self.service.travel_service, "get_trip_context", side_effect=RuntimeError):
            self.service.monitor_trips()
        self.data["records"][0]["segments"][0]["booking_status"] = "CANCELLED"
        self.write()
        self.assertEqual(self.service.monitor_trips()["dispatches"][0]["decision"], "NOT_ELIGIBLE")
        self.data["records"][0]["segments"][0]["booking_status"] = "CONFIRMED"
        self.data["records"][0]["segments"][0].update(departure_date="2026-09-14", scheduled_departure="2026-09-14T19:00")
        self.write()
        self.assertEqual(self.service.monitor_trips()["dispatches"][0]["decision"], "NOT_ELIGIBLE")
        self.data["records"] = []
        self.write()
        self.assertEqual(self.service.monitor_trips()["dispatches"], [])
        self.assertEqual(self.service.repository.connection.execute("SELECT count(*) FROM planning_attempts").fetchone()[0], 1)

    def test_interruption_failed_refresh_does_not_retry(self):
        from unittest.mock import patch
        with patch.object(self.service.repository, "finish_attempt", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.service.monitor_trips()
        self.data["records"].append(None)
        self.write()
        result = self.service.monitor_trips()
        self.assertEqual(result["refresh"]["status"], "REJECTED")
        self.assertEqual(result["dispatches"], [])
        self.assertEqual(self.service.repository.automatic_state("fixture/TRIP-001/OUT")["state"], "FAILED")

    def test_completed_suppression_reopen_and_update(self):
        first = self.service.monitor_trips()["dispatches"][0]
        path = self.service.repository.path
        self.service.repository.close()
        self.service.repository = ItineraryRepository(path)
        self.data["records"][0]["segments"][0]["scheduled_departure"] = "2026-09-11T20:00"
        self.write()
        self.service.clock = FixedClock(datetime(2026, 9, 10, 20))
        with patch.object(self.service.travel_service, "get_trip_context", side_effect=AssertionError("Duplicate lookup")) as lookup:
            result = self.service.monitor_trips()["dispatches"][0]
            lookup.assert_not_called()
        self.assertEqual(result["decision"], "SUPPRESS_DUPLICATE")
        self.assertEqual(result["current_itinerary_revision"], 2)
        self.assertEqual(result["attempt"], first["attempt"])

    def test_failed_then_retry_once(self):
        with patch.object(self.service.travel_service, "get_trip_context", side_effect=RuntimeError("private")) as lookup:
            first = self.service.monitor_trips()["dispatches"][0]
            lookup.assert_called_once()
        self.assertEqual(first["automatic_state_after"], "FAILED")
        self.assertNotIn("private", str(first))
        second = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual((second["decision"], second["attempt"]["attempt_number"]), ("RETRY", 2))
        self.assertEqual(second["automatic_state_after"], "COMPLETED")

    def test_real_no_feasible_is_completed(self):
        # A real V7 search with a strict injected gate policy; production default stays 15.
        self.service.travel_service.coordinator.planner.policy = PlanningPolicy(24 * 60)
        first = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual(first["attempt"]["planning_result"]["status"], "NO_FEASIBLE_PLAN")
        self.assertEqual(first["automatic_state_after"], "COMPLETED")
        self.assertEqual(self.service.monitor_trips()["dispatches"][0]["decision"], "SUPPRESS_DUPLICATE")

    def test_replacement_independent(self):
        self.service.monitor_trips()
        self.data["records"][0]["segments"][0]["segment_id"] = "NEW"
        self.write()
        result = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual((result["decision"], result["attempt"]["attempt_number"]), ("PLAN", 1))

    def test_interrupted_recovered_then_retry(self):
        now = self.service.clock.now()
        self.service.repository.apply_snapshot(self.service.agent.refresh(), as_of=now)
        segment = self.service.repository.list_current_segments()[0]
        self.service.repository.initialize_automatic_state(segment.segment_id, as_of=now)
        activation = self.service.evaluator.evaluate(segment, now)
        provenance = PlanningProvenance("AUTOMATIC", "AUTOMATIC_LEAD_TIME", now, segment.itinerary_id, segment.segment_id, 1)
        attempt_id = self.service.repository.start_attempt(segment, activation, provenance)
        result = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual(result["decision"], "RETRY")
        self.assertEqual(result["attempt"]["attempt_number"], 2)
        old = self.service.repository.get_attempt(attempt_id)
        self.assertEqual((old.state, old.error["code"]), ("FAILED", "INTERRUPTED_EXECUTION"))

    def test_failed_departed_not_retried(self):
        with patch.object(self.service.travel_service, "get_trip_context", side_effect=RuntimeError):
            self.service.monitor_trips()
        self.service.clock = FixedClock(datetime(2026, 9, 11, 19))
        result = self.service.monitor_trips()["dispatches"][0]
        self.assertEqual((result["decision"], result["automatic_state_after"]), ("NOT_ELIGIBLE", "FAILED"))
