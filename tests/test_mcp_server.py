"""MCP contract tests, including a real server subprocess over stdio."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.planning.critic import PlanCritic
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool

MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
if MCP_AVAILABLE:
    from mcp import Client, StdioServerParameters
    from travel_agent.adapters.mcp_server import create_server

ROOT = Path(__file__).resolve().parents[1]


def make_service():
    return TravelService(TravelCoordinator(
        FakeFlightTool(), FakeCalendarTool(), BeamSearchPlanner(2, 3),
    ))


def host_candidates():
    return [
        {"label": label, "leave_time": f"2026-09-11T{time}",
         "summary": f"Host proposes {time}", "history": ["Simulated host"],
         "score": 999999}
        for label, time in [("Host early", "15:50"), ("Host buffer", "16:20"),
                            ("Host meeting", "17:00")]
    ]


@unittest.skipUnless(MCP_AVAILABLE, "Optional MCP SDK missing; install requirements-mcp.txt")
class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    def assert_error(self, result, code):
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error"]["code"], code)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        wire_text = result.model_dump_json(by_alias=True)
        for forbidden in ("Traceback", "ValueError", "RuntimeError", "private-marker", str(ROOT)):
            self.assertNotIn(forbidden, wire_text)

    async def test_discovery_exposes_only_two_described_read_only_tools(self):
        async with Client(create_server()) as client:
            result = await client.list_tools()
        tools = {tool.name: tool for tool in result.tools}
        self.assertEqual(set(tools), {"get_trip_context", "evaluate_trip_plans"})
        self.assertEqual(tools["get_trip_context"].input_schema["required"],
                         ["flight_number", "date"])
        self.assertEqual(tools["evaluate_trip_plans"].input_schema["required"], ["context"])
        self.assertIn("candidates", tools["evaluate_trip_plans"].input_schema["properties"])
        for tool in tools.values():
            self.assertTrue(tool.description)
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertIsNotNone(tool.output_schema)

    async def test_malformed_calls_are_sanitized_before_service_invocation(self):
        service = Mock(spec=TravelService)
        bad_calls = [
            ("get_trip_context", {}),
            ("get_trip_context", {"flight_number": ["private-marker"], "date": "2026-09-11"}),
            ("get_trip_context", {"flight_number": "DL1425", "date": "2026-09-11", "extra": 1}),
            ("evaluate_trip_plans", {}),
            ("evaluate_trip_plans", {"context": "private-marker"}),
            ("evaluate_trip_plans", {"context": {}, "candidates": []}),
            ("evaluate_trip_plans", {"context": {}, "candidates": ["private-marker"]}),
        ]
        async with Client(create_server(service)) as client:
            for name, arguments in bad_calls:
                with self.subTest(name=name, arguments=arguments):
                    self.assert_error(await client.call_tool(name, arguments), "INVALID_INPUT")
        service.get_trip_context.assert_not_called()
        service.evaluate_trip_plans.assert_not_called()

    async def test_domain_validation_errors_are_sanitized(self):
        async with Client(create_server()) as client:
            for name, arguments in [
                ("get_trip_context", {"flight_number": "DL1425", "date": "private-marker"}),
                ("evaluate_trip_plans", {"context": {}}),
            ]:
                self.assert_error(await client.call_tool(name, arguments), "INVALID_INPUT")

    async def test_unexpected_failure_and_unknown_tool_are_sanitized(self):
        service = Mock(spec=TravelService)
        service.get_trip_context.side_effect = RuntimeError("private-marker /internal/path")
        async with Client(create_server(service)) as client:
            result = await client.call_tool("get_trip_context", {
                "flight_number": "DL1425", "date": "2026-09-11",
            })
            self.assert_error(result, "INTERNAL_ERROR")
            self.assert_error(await client.call_tool("private-marker", {}), "UNKNOWN_TOOL")

    async def test_real_stdio_round_trip(self):
        # This launches the actual module, not an in-memory server or mocked transport.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "travel_agent.adapters.mcp_server"],
            cwd=str(ROOT),
        )
        async with asyncio.timeout(45):
            async with Client(params, read_timeout_seconds=15) as client:
                tools = await client.list_tools()
                self.assertEqual({t.name for t in tools.tools},
                                 {"get_trip_context", "evaluate_trip_plans"})
                context_result = await client.call_tool("get_trip_context", {
                    "flight_number": "DL1425", "date": "2026-09-11",
                })
                self.assertFalse(context_result.is_error)
                context = context_result.structured_content
                self.assertEqual(context, make_service().get_trip_context("DL1425", "2026-09-11"))

                baseline = await client.call_tool("evaluate_trip_plans", {"context": context})
                self.assertFalse(baseline.is_error)
                self.assertEqual(baseline.structured_content, make_service().evaluate_trip_plans(context))
                selected = baseline.structured_content["selected_plan"]
                self.assertEqual((selected["leave_time"], selected["score"]),
                                 ("2026-09-11T16:15", 76.4))
                explicit_null = await client.call_tool("evaluate_trip_plans", {
                    "context": context, "candidates": None,
                })
                self.assertEqual(explicit_null.structured_content, baseline.structured_content)

                candidates = host_candidates()
                host = await client.call_tool("evaluate_trip_plans", {
                    "context": context, "candidates": candidates,
                })
                self.assertFalse(host.is_error)
                self.assertEqual(host.structured_content["mode"], "host-assisted")
                self.assertEqual(host.structured_content,
                                 make_service().evaluate_trip_plans(context, candidates))
                unscored = copy.deepcopy(candidates)
                for plan in unscored:
                    del plan["score"]
                clean = await client.call_tool("evaluate_trip_plans", {
                    "context": context, "candidates": unscored,
                })
                self.assertEqual(clean.structured_content, host.structured_content)
                typed_context = make_service().coordinator.get_trip_context("DL1425", "2026-09-11")
                for plan in host.structured_content["finalists"]:
                    self.assertEqual(plan["score"], PlanCritic().score(typed_context, plan))
                self.assertEqual(host.structured_content["selected_plan"]["score"], 76.8)

                for name, arguments in [
                    ("get_trip_context", {}),
                    ("get_trip_context", {"flight_number": 42, "date": "2026-09-11"}),
                    ("get_trip_context", {"flight_number": "DL1425", "date": "private-marker"}),
                    ("evaluate_trip_plans", {"context": {}}),
                    ("evaluate_trip_plans", {"context": context, "candidates": []}),
                    ("evaluate_trip_plans", {"context": context, "candidates": [
                        {"label": "Bad", "leave_time": "private-marker", "summary": "Bad time"},
                    ]}),
                ]:
                    with self.subTest(name=name, arguments=arguments):
                        self.assert_error(await client.call_tool(name, arguments), "INVALID_INPUT")
                # A malformed call must not take down the server.
                recovered = await client.call_tool("evaluate_trip_plans", {"context": context})
                self.assertEqual(recovered.structured_content, baseline.structured_content)
                print("\nMCP stdio verified: 2 tools; context retrieved; baseline 16:15 / 76.40; "
                      "host-assisted 16:20 / 76.80; forged scores ignored; "
                      "6 malformed calls sanitized; server recovered.")


class OptionalDependencyTests(unittest.TestCase):
    def test_both_demos_work_without_site_packages(self):
        # -S removes site-packages, so neither MCP nor its dependencies are available.
        for filename, expected in [("app.py", "76.40"), ("host_assisted_demo.py", "76.80")]:
            with self.subTest(filename=filename):
                result = subprocess.run(
                    [sys.executable, "-B", "-S", filename], cwd=ROOT,
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
