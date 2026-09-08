from travel_agent.agents.errors import InvalidInputError, InvalidDataError, DataUnavailableError, ToolFailureError
import copy
from dataclasses import replace
import unittest
from unittest.mock import Mock

from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.tools.flight_tool import FakeFlightTool


class FlightAgentTests(unittest.TestCase):
    def setUp(self):
        self.flight = FakeFlightTool().get_flight("DL1425", "2026-09-11")
        self.tool = Mock(spec=["get_flight"])
        self.tool.get_flight.return_value = self.flight
        self.agent = FlightAgent(self.tool)

    def test_simulated_dl1425(self):
        result = FlightAgent(FakeFlightTool()).get_flight("DL1425", "2026-09-11")
        self.assertEqual((result.flight_number, result.origin, result.destination),
                         ("DL1425", "ATL", "PHL"))
        self.assertEqual(result.departure_time, "2026-09-11T19:00:00")
        self.assertEqual(result.boarding_time, "2026-09-11T18:30:00")
        self.assertEqual((result.status, result.gate, result.delay_minutes), ("ON TIME", "B18", 0))

    def test_normalizes_request_and_returns_copy(self):
        original = copy.deepcopy(self.flight)
        result = self.agent.get_flight(" dl1425 ", "2026-09-11")
        self.tool.get_flight.assert_called_once_with("DL1425", "2026-09-11")
        self.assertIsNot(result, self.flight)
        self.assertEqual(self.flight, original)

    def test_required_fields(self):
        for field in ("flight_number", "origin", "destination", "status", "gate"):
            for value in (None, "", "   ", 42):
                with self.subTest(field=field, value=value), self.assertRaises(InvalidDataError):
                    self.tool.get_flight.return_value = replace(self.flight, **{field: value})
                    self.agent.get_flight("DL1425", "2026-09-11")

    def test_invalid_times(self):
        for field in ("departure_time", "boarding_time"):
            for value in (None, "bad", "2026-09-11", "2026-09-11T18:00Z"):
                with self.subTest(field=field, value=value), self.assertRaises(InvalidDataError):
                    self.tool.get_flight.return_value = replace(self.flight, **{field: value})
                    self.agent.get_flight("DL1425", "2026-09-11")

    def test_boarding_after_departure(self):
        self.tool.get_flight.return_value = replace(self.flight, boarding_time="2026-09-11T19:01")
        with self.assertRaisesRegex(InvalidDataError, "Boarding time"):
            self.agent.get_flight("DL1425", "2026-09-11")

    def test_mismatched_flight_or_date(self):
        for changes in ({"flight_number": "AA123"}, {"departure_time": "2026-09-12T19:00"}):
            with self.subTest(changes=changes), self.assertRaises(InvalidDataError):
                self.tool.get_flight.return_value = replace(self.flight, **changes)
                self.agent.get_flight("DL1425", "2026-09-11")

    def test_invalid_delay(self):
        for value in (-1, True, "10", 2.5):
            with self.subTest(value=value), self.assertRaises(InvalidDataError):
                self.tool.get_flight.return_value = replace(self.flight, delay_minutes=value)
                self.agent.get_flight("DL1425", "2026-09-11")

    def test_invalid_request_before_retrieval(self):
        for number, date in [(None, "2026-09-11"), ("DL1425", "bad"), ("DL1425", "20260911")]:
            with self.subTest(number=number, date=date), self.assertRaises(InvalidInputError):
                self.agent.get_flight(number, date)
        self.tool.get_flight.assert_not_called()

    def test_invalid_or_unavailable_tool_data(self):
        for value in ({}, []):
            self.tool.get_flight.return_value = value
            with self.subTest(value=value), self.assertRaises(InvalidDataError):
                self.agent.get_flight("DL1425", "2026-09-11")
        self.tool.get_flight.side_effect = OSError("offline")
        with self.assertRaises(ToolFailureError):
            self.agent.get_flight("DL1425", "2026-09-11")
