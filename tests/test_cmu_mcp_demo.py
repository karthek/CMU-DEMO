"""Real local stdio rehearsal, parity, unchanged schemas, and visible failures."""
import ast
import asyncio
from copy import deepcopy
import inspect
import os
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from travel_agent.adapters.mcp_server import TOOLS
from demo.cmu_demo import run_demo
from demo import mcp_host_demo as host


def test_real_stdio_offline_parity_repeatability_and_schemas():
    # Enforce no network/model/provider access in the server subprocess too.
    script = '''
import sys, runpy
def audit(event, args):
    if event == "socket.getaddrinfo" or (event == "socket.connect" and args[1][0] not in ("127.0.0.1", "::1")):
        raise RuntimeError("Network forbidden")
    if event == "import" and (args[0].startswith("travel_agent.live") or args[0].split(".")[0] in
                             {"openai", "anthropic", "google", "langchain", "langgraph"}):
        raise RuntimeError("Provider/model import forbidden")
sys.addaudithook(audit)
runpy.run_module("demo.mcp_demo_server", run_name="__main__")
'''
    params = StdioServerParameters(command=sys.executable, args=["-B", "-c", script], cwd=str(host.ROOT))

    async def exercise():
        first, second = await host.rehearse(params), await host.rehearse(params)
        assert first == second
        assert host.render_host(first) == host.render_host(second)
        async with Client(params) as client:
            discovered = (await client.list_tools()).tools
        assert {t.name: (t.input_schema, t.output_schema) for t in discovered} == {
            t.name: (t.input_schema, t.output_schema) for t in TOOLS}
        return first

    original_connect = socket.socket.connect
    def local_only(sock, address):
        # Windows asyncio's internal socketpair needs loopback, not internet access.
        assert address[0] in ("127.0.0.1", "::1"), "External network forbidden"
        return original_connect(sock, address)
    system_environment = {k: v for k, v in os.environ.items()
                          if k.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}}
    with patch.dict("os.environ", system_environment, clear=True), \
         patch("socket.socket.connect", local_only), \
         patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")):
        report = asyncio.run(exercise())
    direct = run_demo()
    assert report["booked_result"] == direct["booked_result"]
    assert report["planning_result"] == direct["booked_result"]["run"]["planning_result"]
    assert report["context"] == direct["booked_result"]["run"]["context"]
    assert report["proposals"] == direct["calendar_proposals"]
    assert direct["beam_width"] == 2 and direct["depth"] == 3
    assert report["proposals"][0].approval_status == "AWAITING_USER_APPROVAL"
    assert report["proposals"][0].execution_status == "NOT_EXECUTED"
    assert set(report["tools"]) == host.EXPECTED_TOOLS


@pytest.mark.parametrize("fault", ["error", "schema", "missing_payload"])
def test_bad_mcp_response_is_rejected(fault):
    tool = next(t for t in TOOLS if t.name == "evaluate_trip_plans")
    response = SimpleNamespace(is_error=fault == "error", structured_content=None if fault == "missing_payload" else {})
    client = SimpleNamespace(call_tool=AsyncMock(return_value=response))
    with pytest.raises(host.RehearsalFailure):
        asyncio.run(host.checked_call(client, {tool.name: tool}, tool.name, {"context": {}}))


@pytest.mark.parametrize("fault", ["discovery", "missing_tool", "baseline"])
def test_discovery_and_baseline_failures_are_visible(fault):
    direct = run_demo()
    run = direct["booked_result"]["run"]
    responses = {"get_trip_context": run["context"], "evaluate_trip_plans": run["planning_result"],
                 "plan_booked_trip": direct["booked_result"]}
    if fault == "baseline":
        responses = deepcopy(responses)
        responses["evaluate_trip_plans"]["finalists"][0]["score"] = 999
        responses["plan_booked_trip"]["run"]["planning_result"] = responses["evaluate_trip_plans"]
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.list_tools.return_value = SimpleNamespace(tools=TOOLS[:-1] if fault == "missing_tool" else TOOLS)
    if fault == "discovery":
        client.list_tools.side_effect = RuntimeError("discovery failed")
    async def call(name, arguments):
        return SimpleNamespace(is_error=False, structured_content=responses[name])
    client.call_tool.side_effect = call
    with patch.object(host, "Client", return_value=client), pytest.raises(host.RehearsalFailure):
        asyncio.run(host.rehearse())


def test_failed_server_start_has_no_direct_fallback():
    params = StdioServerParameters(command=sys.executable, args=["-B", "-c", "raise SystemExit(1)"], cwd=str(host.ROOT))
    with pytest.raises(host.RehearsalFailure, match="server startup / connection"):
        asyncio.run(host.rehearse(params))


def test_host_has_no_direct_planning_import_or_scoring_implementation():
    source = inspect.getsource(host)
    tree = ast.parse(source)
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name.startswith("travel_agent") or name == "demo.cmu_demo" for name in imports)
    assert not {"TravelService", "BeamSearchPlanner", "PlanCritic", "run_demo"}.intersection(
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    assert "POST-MCP; NOT A PRODUCTION MCP TOOL" in source
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in {"score", "evaluate", "search", "search_outcome", "_rank"}
                   for node in ast.walk(tree))
