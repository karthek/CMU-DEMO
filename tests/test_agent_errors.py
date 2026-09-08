import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from travel_agent.agents.errors import (
    TravelAgentError, InvalidInputError, DataUnavailableError, InvalidDataError, ToolFailureError,
)
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.models import CalendarEvent
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.transport_tool import FakeTransportTool


class AgentErrorTests(unittest.TestCase):
    def adapters(self):
        flight = Mock(spec=["get_flight"])
        calendar = Mock(spec=["get_events"])
        transport = Mock(spec=["get_estimate"])
        return [
            ("flight", flight.get_flight, lambda: FlightAgent(flight).get_flight("DL1425", "2026-09-11")),
            ("calendar", calendar.get_events, lambda: CalendarAgent(calendar).get_events("2026-09-11")),
            ("transport", transport.get_estimate, lambda: TransportAgent(transport).get_estimate()),
        ]

    def assert_safe(self, error, code, agent):
        self.assertIsInstance(error, TravelAgentError)
        self.assertEqual(error.code, code)
        self.assertEqual(error.agent, agent)
        payload = error.to_dict()
        self.assertEqual(set(payload), {"code", "message", "agent"})
        self.assertEqual(payload, {"code": code, "message": str(error), "agent": agent})
        for text in (json.dumps(payload), str(error), repr(error)):
            for forbidden in ("secret-marker", "Traceback", "C:/internal", "password="):
                self.assertNotIn(forbidden, text)

    def test_stable_hierarchy_and_codes(self):
        for cls, code in [(InvalidInputError, "INVALID_INPUT"),
                          (DataUnavailableError, "DATA_UNAVAILABLE"),
                          (InvalidDataError, "INVALID_DATA"), (ToolFailureError, "TOOL_FAILURE")]:
            with self.subTest(cls=cls):
                self.assertTrue(issubclass(cls, TravelAgentError))
                self.assertEqual(cls.code, code)

    def test_none_is_unavailable_for_each_agent(self):
        for agent, method, call in self.adapters():
            method.return_value = None
            with self.subTest(agent=agent), self.assertRaises(DataUnavailableError) as caught:
                call()
            self.assert_safe(caught.exception, "DATA_UNAVAILABLE", agent)
            method.assert_called_once()

    def test_wrong_structure_is_invalid_data_for_each_agent(self):
        for agent, method, call in self.adapters():
            method.return_value = {"secret-marker": "C:/internal"}
            with self.subTest(agent=agent), self.assertRaises(InvalidDataError) as caught:
                call()
            self.assert_safe(caught.exception, "INVALID_DATA", agent)

    def test_tool_failures_are_chained_but_not_serialized(self):
        for agent, method, call in self.adapters():
            original = OSError("password=secret-marker C:/internal Traceback")
            method.side_effect = original
            with self.subTest(agent=agent), self.assertRaises(ToolFailureError) as caught:
                call()
            self.assertIs(caught.exception.__cause__, original)
            self.assert_safe(caught.exception, "TOOL_FAILURE", agent)
            method.assert_called_once()

    def test_process_control_exceptions_propagate(self):
        for agent, method, call in self.adapters():
            method.side_effect = KeyboardInterrupt()
            with self.subTest(agent=agent), self.assertRaises(KeyboardInterrupt):
                call()

    def test_flight_invalid_request(self):
        tool = Mock(spec=["get_flight"])
        for number in ("", None, 42, "secret-marker", "???"):
            with self.subTest(number=number), self.assertRaises(InvalidInputError) as caught:
                FlightAgent(tool).get_flight(number, "2026-09-11")
            self.assert_safe(caught.exception, "INVALID_INPUT", "flight")
        tool.get_flight.assert_not_called()

    def test_boarding_order_payload(self):
        tool = Mock(spec=["get_flight"])
        tool.get_flight.return_value = replace(FakeFlightTool().get_flight("DL1425", "2026-09-11"),
                                              boarding_time="2026-09-11T20:00")
        with self.assertRaises(InvalidDataError) as caught:
            FlightAgent(tool).get_flight("DL1425", "2026-09-11")
        self.assertEqual(caught.exception.to_dict(), {
            "code": "INVALID_DATA", "message": "Boarding time cannot be after departure time.", "agent": "flight",
        })

    def test_calendar_request_times_are_invalid_input(self):
        tool = Mock(spec=["get_events"])
        agent = CalendarAgent(tool)
        for call in [lambda: agent.get_events("secret-marker"),
                     lambda: agent.get_events("9999-12-31"),
                     lambda: agent.departure_conflicts([], "secret-marker"),
                     lambda: agent.free_windows([], "secret-marker", "2026-09-11T18:00"),
                     lambda: agent.free_windows([], "2026-09-11T18:00", "2026-09-11T17:00")]:
            with self.assertRaises(InvalidInputError) as caught:
                call()
            self.assert_safe(caught.exception, "INVALID_INPUT", "calendar")
        tool.get_events.assert_not_called()

    def test_calendar_bad_event_is_invalid_data(self):
        tool = Mock(spec=["get_events"])
        for start, end in [("secret-marker", "2026-09-11T18:00"),
                           ("2026-09-11T18:00", "2026-09-11T17:00")]:
            tool.get_events.return_value = [CalendarEvent("Meeting", start, end)]
            with self.assertRaises(InvalidDataError) as caught:
                CalendarAgent(tool).get_events("2026-09-11")
            self.assert_safe(caught.exception, "INVALID_DATA", "calendar")

    def test_empty_calendar_is_valid(self):
        tool = Mock(spec=["get_events"])
        tool.get_events.return_value = []
        self.assertEqual(CalendarAgent(tool).get_events("2026-09-11"), [])

    def test_negative_transport_components_are_invalid_data(self):
        tool = Mock(spec=["get_estimate"])
        for field in ("travel_minutes", "parking_minutes", "terminal_walk_minutes"):
            tool.get_estimate.return_value = replace(FakeTransportTool().get_estimate(), **{field: -1})
            with self.subTest(field=field), self.assertRaises(InvalidDataError) as caught:
                TransportAgent(tool).get_estimate()
            self.assert_safe(caught.exception, "INVALID_DATA", "transport")

    def test_invalid_timestamp_cause_remains_internal(self):
        tool = Mock(spec=["get_flight"])
        tool.get_flight.return_value = replace(FakeFlightTool().get_flight("DL1425", "2026-09-11"),
                                              boarding_time="secret-marker")
        with self.assertRaises(InvalidDataError) as caught:
            FlightAgent(tool).get_flight("DL1425", "2026-09-11")
        self.assertIsInstance(caught.exception.__cause__, ValueError)
        self.assert_safe(caught.exception, "INVALID_DATA", "flight")
