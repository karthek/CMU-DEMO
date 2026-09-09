from dataclasses import replace
from unittest.mock import patch
from test_itinerary_service import ServiceFixture
from travel_agent.models import FlightState
from travel_agent.tools.fixture_flight_tool import FixtureFlightTool


class FlightFixtureTests(ServiceFixture):
    def test_exact_missing_and_conflict(self):
        tool = FixtureFlightTool(self.flights)
        self.assertEqual(tool.get_flight("DL1425", "2026-09-11").destination, "PHL")
        self.assertIsNone(tool.get_flight("AA123", "2026-09-11"))
        duplicate = dict(self.flight_data["flights"][0], gate="B99")
        self.flight_data["flights"].append(duplicate)
        self.write()
        with self.assertRaises(ValueError):
            tool.get_flight("DL1425", "2026-09-11")

    def test_time_basis_and_bad_times(self):
        tool = FixtureFlightTool(self.flights)
        for basis in ("UTC", None):
            self.flight_data["time_basis"] = basis
            self.write()
            with self.assertRaises(ValueError):
                tool.get_flight("DL1425", "2026-09-11")
        self.flight_data["time_basis"] = "America/New_York"
        for time in ("2026-09-11T19:00-04:00", "2026-11-01T01:30", "2026-03-08T02:30"):
            self.flight_data["flights"][0]["departure_time"] = time
            self.write()
            with self.assertRaises(ValueError):
                tool.get_flight("DL1425", "2026-09-11")

    def test_mismatched_identity_and_route(self):
        original = FlightState(**self.flight_data["flights"][0])
        tool = self.service.travel_service.coordinator.flight_agent._flight_tool
        for changes, expected in [({"flight_number": "AA123"}, "BOOKING_FLIGHT_IDENTITY_MISMATCH"),
                                  ({"departure_time": "2026-09-12T19:00"}, "BOOKING_FLIGHT_IDENTITY_MISMATCH"),
                                  ({"origin": "JFK"}, "BOOKING_FLIGHT_ROUTE_MISMATCH")]:
            with patch.object(tool, "get_flight", return_value=replace(original, **changes)):
                result = self.service.plan_booked_trip(selector={})
            self.assertEqual(result["run"]["error"]["code"], expected)

    def test_operational_time_wins(self):
        self.flight_data["flights"][0].update(departure_time="2026-09-11T20:00", boarding_time="2026-09-11T19:30")
        self.write()
        attempt = self.service.monitor_trips()["dispatches"][0]["attempt"]
        self.assertEqual(attempt["activation_result"]["scheduled_departure"], "2026-09-11T19:00:00")
        self.assertEqual(attempt["context"]["flight"]["departure_time"], "2026-09-11T20:00")
        self.assertEqual(attempt["planning_result"]["selected_plan"]["feasibility"]["gate_deadline"], "2026-09-11T19:45:00")
