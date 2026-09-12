"""Neutral stdio host; no direct planning/service fallback.

Run: .venv/Scripts/python.exe -B demo/mcp_host_demo.py
The numeric baseline below is a rehearsal assertion, never a computed/replaced
answer. Displayed evaluations always come from MCP structured_content.
"""
import asyncio
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from demo.hitl import propose_calendar_changes, render_hitl

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {"get_trip_context", "evaluate_trip_plans", "monitor_trips", "plan_booked_trip"}


class RehearsalFailure(RuntimeError):
    pass


def server_parameters():
    return StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "demo.mcp_demo_server"], cwd=str(ROOT))


async def checked_call(client, tools, name, arguments):
    try:
        tool = tools[name]
        Draft202012Validator(tool.input_schema).validate(arguments)
        response = await client.call_tool(name, arguments)
        if response.is_error:
            raise RehearsalFailure(f"{name}: MCP tool returned an error")
        payload = response.structured_content
        if not isinstance(payload, dict) or tool.output_schema is None:
            raise RehearsalFailure(f"{name}: missing structured result/schema")
        Draft202012Validator(tool.output_schema).validate(payload)
        return payload
    except RehearsalFailure:
        raise
    except Exception as exc:
        raise RehearsalFailure(f"{name}: call or schema validation failed") from exc


async def rehearse(parameters=None):
    scenario = json.loads((ROOT / "demo/scenario.json").read_text(encoding="utf-8"))
    stage = "server startup / connection"
    try:
        async with asyncio.timeout(45), Client(parameters or server_parameters()) as client:
            stage = "tool discovery"
            discovery = await client.list_tools()
            tools = {tool.name: tool for tool in discovery.tools}
            if len(discovery.tools) != len(EXPECTED_TOOLS) or set(tools) != EXPECTED_TOOLS:
                raise RehearsalFailure("tool discovery: expected exactly the four existing travel tools")
            segment = scenario["itineraries"]["records"][0]["segments"][0]
            stage = "get_trip_context"
            context = await checked_call(client, tools, stage,
                {"flight_number": segment["flight_number"], "date": segment["departure_date"]})
            stage = "evaluate_trip_plans"
            result = await checked_call(client, tools, stage,
                {"context": context, "candidates": scenario["candidates"]})
            stage = "plan_booked_trip"
            booked = await checked_call(client, tools, stage,
                {"selector": {}, "candidates": scenario["candidates"]})
            stage = "authoritative baseline validation"
            if (booked["status"] != "COMPLETED" or booked["run"]["context"] != context
                or booked["run"]["planning_result"] != result or booked["as_of"] != scenario["as_of"]):
                raise RehearsalFailure("booked-trip/context evaluation parity failed")
            expected = [("2026-09-16T16:20", 76.8), ("2026-09-16T16:10", 76.0)]
            if (result["status"] != "PLAN_FOUND" or
                [(p["leave_time"], p["score"]) for p in result["finalists"]] != expected
                or result["selected_plan"] != result["finalists"][0]):
                raise RehearsalFailure("authoritative result differs from CP1-CP3 baseline")
            proposals = propose_calendar_changes(context, result, scenario["calendar_proposal_slots"],
                                                 as_of=scenario["as_of"])
            if not proposals:
                raise RehearsalFailure("expected demo governance proposal missing")
            return {"context": context, "planning_result": result, "booked_result": booked,
                    "proposals": proposals, "tools": tuple(sorted(tools))}
    except RehearsalFailure:
        raise
    except Exception as exc:
        raise RehearsalFailure(f"{stage}: rehearsal failed ({type(exc).__name__})") from exc


def render_host(report):
    result = report["planning_result"]
    selected = result["selected_plan"]
    alternative = result["finalists"][1]
    return "\n".join([
        "=== HOST / MCP REHEARSAL ===", "Host: Neutral MCP Client (no LLM)",
        "Transport: local stdio", "Scenario: CMU Demo / FIXTURE / fixed clock",
        "=== MCP TOOL FLOW ===", "Discovery: " + ", ".join(report["tools"]),
        "1. get_trip_context -> context received",
        "2. evaluate_trip_plans -> host-style proposals evaluated",
        "3. plan_booked_trip -> booked-trip result received; parity confirmed",
        "=== AUTHORITATIVE RESULT [MCP] ===",
        f"Recommended: {selected['leave_time']} / {selected['score']:.2f}",
        f"Alternative: {alternative['leave_time']} / {alternative['score']:.2f}",
        f"Gate feasibility: {selected['feasibility']['feasible']}; "
        f"modeled arrival {selected['feasibility']['gate_arrival_time']}; "
        f"deadline {selected['feasibility']['gate_deadline']}",
        "Best within explored candidates; gate feasibility is relative to configured assumptions.",
        "=== DEMO GOVERNANCE PRESENTATION [POST-MCP; NOT A PRODUCTION MCP TOOL] ===",
        render_hitl(report["proposals"]),
        "=== ARCHITECTURE BOUNDARY ===", "Host: proposes / invokes / communicates",
        "MCP travel agent: validates / evaluates / constrains",
        "CP3 demo policy: creates immutable approval-required proposals from returned conflict records.",
        "Calendar mutation: not exposed. plan_booked_trip writes only temporary local attempt history.",
        "CP2 search trace remains a direct-demo feature; no trace fields were added to MCP schemas.",
    ])


def main():
    try:
        print(render_host(asyncio.run(rehearse())))
    except Exception as exc:
        message = str(exc) if isinstance(exc, RehearsalFailure) else type(exc).__name__
        print(f"MCP REHEARSAL FAILED: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
