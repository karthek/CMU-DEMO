"""Actual MCP stdio with injected clocks at the composition boundary, never public time overrides."""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from travel_agent.adapters.mcp_server import create_server, TOOLS
from test_itinerary_agent import snapshot

ROOT = Path(__file__).resolve().parents[1]
NAMES = {"get_trip_context", "evaluate_trip_plans", "monitor_trips", "plan_booked_trip"}
SCRIPT = """
import asyncio, sys
from datetime import datetime
from mcp.server.stdio import stdio_server
from travel_agent.composition import create_itinerary_service
from travel_agent.itinerary.clock import FixedClock
from travel_agent.adapters.mcp_server import create_server
from travel_agent.planning.policy import PlanningPolicy
service = create_itinerary_service(database_path=sys.argv[1], itinerary_path=sys.argv[2], flight_path=sys.argv[3],
                                   clock=FixedClock(datetime.fromisoformat(sys.argv[4])))
if sys.argv[5] == 'no_plan':
    service.travel_service.coordinator.planner.policy = PlanningPolicy(1440)
async def run():
    server = create_server(itinerary_service=service)
    try:
        async with stdio_server() as streams:
            await server.run(*streams, server.create_initialization_options())
    finally:
        service.repository.close()
asyncio.run(run())
"""


class V8MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = snapshot()
        self.flight_data = json.loads((ROOT / "fixtures/v8/flights.json").read_text())
        self.write()

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        (self.root / "itineraries.json").write_text(json.dumps(self.data), encoding="utf-8")
        (self.root / "flights.json").write_text(json.dumps(self.flight_data), encoding="utf-8")

    def params(self, now="2026-09-10T19:00", mode="normal"):
        return StdioServerParameters(command=sys.executable, args=["-B", "-c", SCRIPT,
            str(self.root / "state.sqlite3"), str(self.root / "itineraries.json"), str(self.root / "flights.json"), now, mode], cwd=str(ROOT))

    async def call(self, client, name, args):
        response = await client.call_tool(name, args)
        self.assertFalse(response.is_error, response)
        payload = response.structured_content
        self.assertEqual(json.loads(response.content[0].text), payload)
        schema = next(t.output_schema for t in TOOLS if t.name == name)
        Draft202012Validator(schema).validate(payload)
        return payload

    async def test_real_stdio_activation_suppression_manual_replan(self):
        async with asyncio.timeout(45), Client(self.params()) as client:
            self.assertEqual({t.name for t in (await client.list_tools()).tools}, NAMES)
            first = await self.call(client, "monitor_trips", {})
            self.assertEqual(first["dispatches"][0]["decision"], "PLAN")
            second = await self.call(client, "monitor_trips", {})
            self.assertEqual(second["dispatches"][0]["decision"], "SUPPRESS_DUPLICATE")
            self.assertEqual(second["dispatches"][0]["attempt"], first["dispatches"][0]["attempt"])
            manual = await self.call(client, "plan_booked_trip", {"selector": {}})
            self.assertEqual(manual["status"], "COMPLETED")
            self.assertEqual(manual["run"]["trigger"], "USER_REQUEST")
        print("V8 stdio: 4 tools; PLAN -> COMPLETED; SUPPRESS_DUPLICATE; manual replan completed.")

    async def test_real_stdio_early_manual_and_zero_activation(self):
        async with asyncio.timeout(45), Client(self.params("2026-09-06T19:00")) as client:
            automatic = await self.call(client, "monitor_trips", {})
            self.assertEqual(automatic["activated_segments"], [])
            manual = await self.call(client, "plan_booked_trip", {"selector": {"destination": "PHL"}})
            self.assertEqual(manual["run"]["activation_result"]["status"], "NOT_YET_ELIGIBLE")
            self.assertEqual(manual["status"], "COMPLETED")
            self.data["records"] = []
            self.write()
            empty = await self.call(client, "monitor_trips", {})
            self.assertEqual(empty["activation_results"], [])
        print("V8 stdio: five-day-early USER_REQUEST completed; automatic NOT_YET_ELIGIBLE; empty snapshot valid.")

    async def test_real_stdio_failure_retry_multiple_and_selection(self):
        second = copy.deepcopy(self.data["records"][0])
        second["itinerary_id"] = "TRIP-002"
        self.data["records"].append(second)
        saved = self.flight_data["flights"]
        self.flight_data["flights"] = []
        self.write()
        async with asyncio.timeout(45), Client(self.params()) as client:
            first = await self.call(client, "monitor_trips", {})
            self.assertEqual([d["automatic_state_after"] for d in first["dispatches"]], ["FAILED", "FAILED"])
            self.flight_data["flights"] = saved
            self.write()
            retried = await self.call(client, "monitor_trips", {})
            self.assertEqual([d["decision"] for d in retried["dispatches"]], ["RETRY", "RETRY"])
            self.assertEqual([d["attempt"]["attempt_number"] for d in retried["dispatches"]], [2, 2])
            self.assertEqual([d["automatic_state_after"] for d in retried["dispatches"]], ["COMPLETED", "COMPLETED"])
            multiple = await self.call(client, "plan_booked_trip", {"selector": {}})
            self.assertEqual(multiple["status"], "NEEDS_SELECTION")
        print("V8 stdio: two missing-flight failures -> RETRY attempt 2 -> both COMPLETED; multiple-match response.")

    async def test_real_stdio_no_feasible_completed(self):
        async with asyncio.timeout(45), Client(self.params(mode="no_plan")) as client:
            result = await self.call(client, "monitor_trips", {})
            attempt = result["dispatches"][0]["attempt"]
            self.assertEqual(attempt["planning_result"]["status"], "NO_FEASIBLE_PLAN")
            self.assertEqual(attempt["state"], "COMPLETED")
            repeated = await self.call(client, "monitor_trips", {})
            self.assertEqual(repeated["dispatches"][0]["decision"], "SUPPRESS_DUPLICATE")
        print("V8 stdio: real V7 NO_FEASIBLE_PLAN -> COMPLETED -> SUPPRESS_DUPLICATE.")

    async def test_real_stdio_reject_overrides_and_recover(self):
        async with asyncio.timeout(45), Client(self.params()) as client:
            for name, args in [("monitor_trips", {"as_of": "private"}), ("monitor_trips", {"eligible": True}),
                               ("monitor_trips", {"provenance": {}}), ("monitor_trips", {"state": "COMPLETED"}),
                               ("plan_booked_trip", {"selector": {}, "as_of": "private"}),
                               ("plan_booked_trip", {"selector": {"segment_id": "private"}}),
                               ("plan_booked_trip", {"selector": {"departure_date": "2026-99-99"}})]:
                response = await client.call_tool(name, args)
                self.assertTrue(response.is_error)
                self.assertEqual(response.structured_content["error"]["code"], "INVALID_INPUT")
                self.assertNotIn("private", str(response.structured_content))
            self.assertTrue((await self.call(client, "monitor_trips", {}))["activated_segments"])

    async def test_unexpected_error_sanitized(self):
        service = Mock()
        service.monitor_trips.side_effect = RuntimeError("private-marker")
        async with Client(create_server(itinerary_service=service)) as client:
            response = await client.call_tool("monitor_trips", {})
        self.assertTrue(response.is_error)
        self.assertEqual(response.structured_content["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("private-marker", str(response.structured_content))

    def test_exact_v7_schemas_unchanged(self):
        source = subprocess.run(["git", "show", "v7:travel_agent/adapters/mcp_server.py"], cwd=ROOT,
                                capture_output=True, text=True, check=True).stdout
        namespace = {"__name__": "v7_schema_reference"}
        exec(compile(source, "v7_reference", "exec"), namespace)
        for before, after in zip(namespace["TOOLS"], TOOLS[:2]):
            self.assertEqual(before.input_schema, after.input_schema)
            self.assertEqual(before.output_schema, after.output_schema)
