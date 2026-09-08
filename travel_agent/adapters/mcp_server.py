"""Local stdio MCP adapter. All travel behavior belongs to TravelService."""

import asyncio
import json
import logging

from jsonschema import Draft202012Validator
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool


READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False,
    idempotent_hint=True, open_world_hint=False,
)
CONTEXT_SCHEMA = {
    "type": "object",
    "description": (
        "Trip context returned by get_trip_context: flight, calendar_events, "
        "airport_travel_minutes, security_minutes, gate_walk_minutes, and "
        "preferred_buffer_minutes. Pass it unchanged for planning against those facts."
    ),
}
CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "minLength": 1},
        "leave_time": {
            "type": "string",
            "description": "Local ISO date/time without timezone, e.g. 2026-09-11T16:20.",
        },
        "summary": {"type": "string", "minLength": 1},
        "history": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["label", "leave_time", "summary"],
    # V2 discards scores and other extra candidate fields rather than trusting them.
    "additionalProperties": True,
}
TOOLS = (
    Tool(
        name="get_trip_context",
        description=(
            "Retrieve flight details, calendar events, and current travel assumptions "
            "before planning when to leave for the airport. Requires a flight number "
            "and YYYY-MM-DD date. V3 returns simulated flight/calendar data, not live "
            "information. Pass the returned context to evaluate_trip_plans. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "flight_number": {"type": "string", "minLength": 1},
                "date": {"type": "string", "description": "Travel date in YYYY-MM-DD format."},
            },
            "required": ["flight_number", "date"],
            "additionalProperties": False,
        },
        output_schema=CONTEXT_SCHEMA,
        annotations=READ_ONLY,
    ),
    Tool(
        name="evaluate_trip_plans",
        description=(
            "Validate, score, refine, and rank airport departure plans using the "
            "travel core's Beam Search and Critic. First obtain context with "
            "get_trip_context. Omit candidates or pass null for deterministic V1 "
            "baseline generation; supply a non-empty list for host-assisted planning. "
            "Host candidates are proposals only: supplied scores are ignored and all "
            "scores are computed by the core. Returns mode, selected_plan, finalists, "
            "and recommendation. Scores are rule-based rankings, not guarantees. "
            "Does not book travel or modify calendars."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "context": CONTEXT_SCHEMA,
                "candidates": {
                    "anyOf": [
                        {"type": "array", "items": CANDIDATE_SCHEMA, "minItems": 1},
                        {"type": "null"},
                    ],
                    "default": None,
                },
            },
            "required": ["context"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "mode": {"enum": ["deterministic", "host-assisted"]},
                "selected_plan": {"type": "object"},
                "finalists": {"type": "array", "items": {"type": "object"}},
                "recommendation": {"type": "string"},
            },
            "required": ["mode", "selected_plan", "finalists", "recommendation"],
        },
        annotations=READ_ONLY,
    ),
)


def _result(data: dict, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, allow_nan=False))],
        structured_content=data,
        is_error=is_error,
    )


def _error(code: str, message: str) -> CallToolResult:
    return _result({"error": {"code": code, "message": message}}, is_error=True)


def create_server(service: TravelService | None = None) -> Server:
    if service is None:
        service = TravelService(TravelCoordinator(
            flight_tool=FakeFlightTool(),
            calendar_tool=FakeCalendarTool(),
            planner=BeamSearchPlanner(beam_width=2, depth=3),
        ))
    tools = {tool.name: tool for tool in TOOLS}
    validators = {name: Draft202012Validator(tool.input_schema) for name, tool in tools.items()}

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=list(TOOLS))

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        if params.name not in tools:
            return _error("UNKNOWN_TOOL", "Choose a tool returned by tools/list.")
        arguments = params.arguments if params.arguments is not None else {}
        invalid_message = (
            "Provide a non-empty flight_number and a valid YYYY-MM-DD date."
            if params.name == "get_trip_context" else
            "Provide a valid trip context and omit candidates or supply a non-empty "
            "list of plans with label, local ISO leave_time, summary, and optional string-list history."
        )
        try:
            # The low-level SDK advertises schemas but leaves argument validation to us.
            if not validators[params.name].is_valid(arguments):
                return _error("INVALID_INPUT", invalid_message)
            if params.name == "get_trip_context":
                data = service.get_trip_context(**arguments)
            else:
                data = service.evaluate_trip_plans(**arguments)
            return _result(data)
        except ValueError:
            return _error("INVALID_INPUT", invalid_message)
        except Exception:
            # Never return exception text, input values, file paths, or tracebacks.
            return _error("INTERNAL_ERROR", "The travel service could not complete this request.")

    return Server(
        "Travel Agent", version="3.0.0",
        instructions=(
            "Local travel-planning prototype using simulated data. Retrieve context, "
            "then evaluate plans. The host owns conversation and model inference."
        ),
        on_list_tools=list_tools, on_call_tool=call_tool,
    )


async def run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # stderr only; stdout is the MCP wire.
    asyncio.run(run_stdio())
