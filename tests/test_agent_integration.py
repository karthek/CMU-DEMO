"""V5 orchestration tests: domain reasoning stays in the V4 agents."""
from dataclasses import asdict, replace
import importlib.util
import unittest
from unittest.mock import Mock, call, patch

from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.agents.errors import (
    InvalidInputError, InvalidDataError, DataUnavailableError, ToolFailureError,
)
from travel_agent.composition import create_simulated_coordinator
from travel_agent.coordinator import TravelCoordinator
from travel_agent.models import TripContext
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.transport_tool import FakeTransportTool


class AgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.flight = Mock(spec=FlightAgent)
        self.calendar = Mock(spec=CalendarAgent)
        self.transport = Mock(spec=TransportAgent)
        self.flight_state = FlightAgent(FakeFlightTool()).get_flight("DL1425", "2026-09-11")
        self.events = CalendarAgent(FakeCalendarTool()).get_events("2026-09-11")
        self.estimate = FakeTransportTool().get_estimate()
        self.flight.get_flight.return_value = self.flight_state
        self.calendar.get_events.return_value = self.events
        self.transport.get_estimate.return_value = self.estimate
        self.planner = Mock(wraps=BeamSearchPlanner(2, 3))
        self.coordinator = TravelCoordinator(
            flight_agent=self.flight, calendar_agent=self.calendar,
            transport_agent=self.transport, planner=self.planner,
        )
        self.service = TravelService(self.coordinator)

    def test_calls_all_agents_in_order(self):
        order = Mock()
        order.attach_mock(self.flight, "flight")
        order.attach_mock(self.calendar, "calendar")
        order.attach_mock(self.transport, "transport")
        self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual(order.mock_calls, [
            call.flight.get_flight("DL1425", "2026-09-11"),
            call.calendar.get_events("2026-09-11"), call.transport.get_estimate(),
        ])
        self.planner.search.assert_not_called()

    def test_validated_agent_outputs_map_without_revalidation(self):
        with patch("travel_agent.agents._validation.timestamp", side_effect=AssertionError("duplicate validation")), \
             patch("travel_agent.contracts.context_from_dict", side_effect=AssertionError("duplicate validation")):
            context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertIs(context.flight, self.flight_state)
        self.assertEqual(context.calendar_events, self.events)
        self.assertIsNot(context.calendar_events[0], self.events[0])
        self.assertEqual([e.priority for e in context.calendar_events], ["normal", "high"])
        self.assertEqual((context.airport_travel_minutes, context.security_minutes,
                          context.gate_walk_minutes, context.preferred_buffer_minutes), (45, 20, 15, 45))

    def test_parking_and_terminal_walk_not_added_anywhere(self):
        self.transport.get_estimate.return_value = replace(
            self.estimate, parking_minutes=100, terminal_walk_minutes=200,
        )
        context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual((context.airport_travel_minutes, context.security_minutes, context.gate_walk_minutes),
                         (45, 20, 15))
        result = self.coordinator.evaluate_trip_plans(context)
        self.assertEqual(result["selected_plan"]["score"], 76.4)

    def test_changed_transport_tool_duration_reaches_context(self):
        tool = Mock(spec=FakeTransportTool)
        tool.get_estimate.return_value = replace(self.estimate, travel_minutes=62)
        coordinator = create_simulated_coordinator()
        coordinator.transport_agent = TransportAgent(tool)
        context = coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual(context.airport_travel_minutes, 62)
        tool.get_estimate.assert_called_once_with()

    def test_unknown_priority_is_not_reinterpreted(self):
        for priority in ("low", "critical", "HIGH"):
            with self.subTest(priority=priority):
                self.calendar.get_events.return_value = [replace(self.events[0], priority=priority)]
                with self.assertRaises(InvalidDataError) as caught:
                    self.service.get_trip_context("DL1425", "2026-09-11")
                self.assertEqual(caught.exception.agent, "calendar")
                self.assertEqual(caught.exception.code, "INVALID_DATA")
        self.transport.get_estimate.assert_not_called()
        self.planner.search.assert_not_called()

    def test_real_calendar_sorting_and_priorities_reach_context(self):
        tool = Mock(spec=FakeCalendarTool)
        tool.get_events.return_value = list(reversed(FakeCalendarTool().get_events("2026-09-11")))
        self.coordinator.calendar_agent = CalendarAgent(tool)
        context = self.coordinator.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual([e.title for e in context.calendar_events], ["Project Review", "Leadership Meeting"])
        self.assertEqual([e.priority for e in context.calendar_events], ["normal", "high"])

    def test_all_agent_error_types_propagate_unchanged_without_fallback(self):
        methods = [("flight", self.flight.get_flight), ("calendar", self.calendar.get_events),
                   ("transport", self.transport.get_estimate)]
        for index, (agent, method) in enumerate(methods):
            for cls in (InvalidInputError, InvalidDataError, DataUnavailableError, ToolFailureError):
                for entry in (self.service.get_trip_context, self.coordinator.plan_trip):
                    for _, reset_method in methods:
                        reset_method.reset_mock()
                    error = cls("Safe domain message.", agent=agent)
                    error.__cause__ = OSError("private-marker C:/internal")
                    method.side_effect = error
                    with self.subTest(agent=agent, cls=cls, entry=entry), self.assertRaises(cls) as caught:
                        entry("DL1425", "2026-09-11")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(caught.exception.to_dict(), error.to_dict())
                    self.assertIs(caught.exception.__cause__, error.__cause__)
                    method.assert_called_once()
                    for _, later in methods[index + 1:]:
                        later.assert_not_called()
                    method.side_effect = None
        self.planner.search.assert_not_called()

    def test_real_tool_failure_propagates_through_agent_and_service(self):
        for attribute, agent_class, tool_method in [
            ("flight_agent", FlightAgent, "get_flight"),
            ("calendar_agent", CalendarAgent, "get_events"),
            ("transport_agent", TransportAgent, "get_estimate"),
        ]:
            with self.subTest(agent=attribute):
                coordinator = create_simulated_coordinator()
                tool = Mock(spec=[tool_method])
                original = OSError("private-marker C:/internal")
                getattr(tool, tool_method).side_effect = original
                setattr(coordinator, attribute, agent_class(tool))
                with self.assertRaises(ToolFailureError) as caught:
                    TravelService(coordinator).get_trip_context("DL1425", "2026-09-11")
                self.assertIs(caught.exception.__cause__, original)
                self.assertNotIn("private-marker", str(caught.exception.to_dict()))

    def test_external_context_still_validated_at_service_boundary(self):
        with self.assertRaises(ValueError):
            self.service.evaluate_trip_plans({})
        self.planner.search.assert_not_called()
        self.flight.get_flight.assert_not_called()

    def test_wire_context_matches_v2_format(self):
        expected = asdict(TripContext(
            flight=FakeFlightTool().get_flight("DL1425", "2026-09-11"),
            calendar_events=FakeCalendarTool().get_events("2026-09-11"),
        ))
        self.assertEqual(self.service.get_trip_context("DL1425", "2026-09-11"), expected)
        self.assertEqual(self.flight_state.boarding_time, "2026-09-11T18:30:00")

    def test_wire_serialization_retains_nonzero_seconds(self):
        self.flight.get_flight.return_value = replace(self.flight_state, boarding_time="2026-09-11T18:30:12.500000")
        context = self.service.get_trip_context("DL1425", "2026-09-11")
        self.assertEqual(context["flight"]["boarding_time"], "2026-09-11T18:30:12.500000")

    def test_legacy_constructor_also_uses_agents(self):
        coordinator = TravelCoordinator(FakeFlightTool(), FakeCalendarTool(), BeamSearchPlanner(2, 3))
        self.assertIsInstance(coordinator.flight_agent, FlightAgent)
        self.assertIsInstance(coordinator.calendar_agent, CalendarAgent)
        self.assertIsInstance(coordinator.transport_agent, TransportAgent)
        self.assertEqual(coordinator.plan_trip("DL1425", "2026-09-11")["selected_plan"]["score"], 76.4)

    def test_agent_constructor_requires_explicit_transport(self):
        with self.assertRaises(TypeError):
            TravelCoordinator(flight_agent=self.flight, calendar_agent=self.calendar, planner=self.planner)

    def test_both_baselines_through_wired_service(self):
        service = TravelService(create_simulated_coordinator())
        context = service.get_trip_context("DL1425", "2026-09-11")
        baseline = service.evaluate_trip_plans(context)
        self.assertEqual((baseline["selected_plan"]["leave_time"], baseline["selected_plan"]["score"]),
                         ("2026-09-11T16:15", 76.4))
        candidates = [{"label": label, "leave_time": f"2026-09-11T{time}",
                       "summary": "Host proposal", "history": [], "score": 999999}
                      for label, time in [("Host early", "15:50"), ("Host buffer", "16:20"), ("Host meeting", "17:00")]]
        result = service.evaluate_trip_plans(context, candidates)
        self.assertEqual([(p["leave_time"], p["score"]) for p in result["finalists"]],
                         [("2026-09-11T16:20", 76.8), ("2026-09-11T16:10", 76.0)])


@unittest.skipUnless(importlib.util.find_spec("mcp"), "Optional MCP SDK missing")
class AgentMCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_errors_keep_existing_mcp_sanitization(self):
        from mcp import Client
        from travel_agent.adapters.mcp_server import create_server
        for cls, expected in [(InvalidInputError, "INVALID_INPUT"), (InvalidDataError, "INTERNAL_ERROR"),
                              (DataUnavailableError, "INTERNAL_ERROR"), (ToolFailureError, "INTERNAL_ERROR")]:
            coordinator = create_simulated_coordinator()
            coordinator.flight_agent = Mock(spec=FlightAgent)
            error = cls("Safe flight error.", agent="flight")
            error.__cause__ = RuntimeError("private-marker C:/internal")
            coordinator.flight_agent.get_flight.side_effect = error
            async with Client(create_server(TravelService(coordinator))) as client:
                result = await client.call_tool("get_trip_context", {"flight_number": "DL1425", "date": "2026-09-11"})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error"]["code"], expected)
                self.assertEqual(set(result.structured_content["error"]), {"code", "message"})
                self.assertNotIn("private-marker", result.model_dump_json())
