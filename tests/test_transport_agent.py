from travel_agent.agents.errors import InvalidInputError, InvalidDataError, DataUnavailableError, ToolFailureError
from dataclasses import replace
import unittest
from unittest.mock import Mock

from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.tools.transport_tool import FakeTransportTool


class TransportAgentTests(unittest.TestCase):
    def setUp(self):
        self.estimate = FakeTransportTool().get_estimate()
        self.tool = Mock(spec=["get_estimate"])
        self.tool.get_estimate.return_value = self.estimate
        self.agent = TransportAgent(self.tool)

    def test_simulated_components(self):
        result = TransportAgent(FakeTransportTool()).get_estimate()
        self.assertEqual(result.travel_minutes, 45)
        self.assertEqual(result.parking_minutes, 10)
        self.assertEqual(result.terminal_walk_minutes, 8)
        self.assertEqual(result.mode, "drive")
        self.assertEqual(result.source, "simulated")

    def test_invalid_durations(self):
        for field in ("travel_minutes", "parking_minutes", "terminal_walk_minutes"):
            for value in (-1, True, "45", 1.5, None):
                with self.subTest(field=field, value=value), self.assertRaises(InvalidDataError):
                    self.tool.get_estimate.return_value = replace(self.estimate, **{field: value})
                    self.agent.get_estimate()

    def test_zero_durations_and_normalized_metadata(self):
        self.tool.get_estimate.return_value = replace(
            self.estimate, travel_minutes=0, parking_minutes=0, terminal_walk_minutes=0,
            mode=" walk ", source=" simulated ",
        )
        result = self.agent.get_estimate()
        self.assertEqual((result.travel_minutes, result.parking_minutes, result.terminal_walk_minutes), (0, 0, 0))
        self.assertEqual((result.mode, result.source), ("walk", "simulated"))

    def test_invalid_metadata(self):
        for field in ("mode", "source"):
            for value in (None, " ", 1):
                with self.subTest(field=field, value=value), self.assertRaises(InvalidDataError):
                    self.tool.get_estimate.return_value = replace(self.estimate, **{field: value})
                    self.agent.get_estimate()

    def test_returns_copy(self):
        result = self.agent.get_estimate()
        self.assertIsNot(result, self.estimate)
        self.assertEqual(result, self.estimate)
        self.tool.get_estimate.assert_called_once_with()

    def test_invalid_or_unavailable_tool_data(self):
        for value in ({}, []):
            self.tool.get_estimate.return_value = value
            with self.subTest(value=value), self.assertRaises(InvalidDataError):
                self.agent.get_estimate()
        self.tool.get_estimate.side_effect = OSError("offline")
        with self.assertRaises(ToolFailureError):
            self.agent.get_estimate()
