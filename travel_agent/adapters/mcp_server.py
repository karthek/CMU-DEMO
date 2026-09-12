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

from travel_agent.agents.errors import InvalidInputError
from travel_agent.composition import create_simulated_coordinator
from travel_agent.service import TravelService


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
                "status": {"enum": ["PLAN_FOUND", "NO_FEASIBLE_PLAN"]},
                "selected_plan": {
                    "type": ["object", "null"],
                    "description": "Feasible plan with core-computed feasibility, score_breakdown and calendar_conflicts; null if bounded search found none.",
                },
                "diagnostics": {"type": "object", "description": "Explored-candidate scope/count; best infeasible candidate is diagnostic only."},
                "finalists": {"type": "array", "items": {"type": "object"}},
                "recommendation": {"type": "string"},
            },
            "required": ["status", "mode", "selected_plan", "finalists", "recommendation", "diagnostics"],
        },
        annotations=READ_ONLY,
    ),
)


from travel_agent.adapters.itinerary_schemas import MONITOR_INPUT, schemas

_manual_input, _monitor_output, _manual_output = schemas(CANDIDATE_SCHEMA, CONTEXT_SCHEMA, TOOLS[1].output_schema)
TOOLS += (
    Tool(name="monitor_trips", description=(
        "Refresh simulated booked itineraries and automatically plan every eligible segment. "
        "The service clock supplies current time. Persists local history; completed automatic runs are suppressed. "
        "No scheduler, notification, calendar write, or booking action."),
        input_schema=MONITOR_INPUT, output_schema=_monitor_output,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)),
    Tool(name="plan_booked_trip", description=(
        "Plan or replan an upcoming booked segment on explicit user request. Refresh and resolve selector first. "
        "Bypasses only automatic lead time; preserves V7 feasibility and scoring. Multiple matches require selection. "
        "Persists local attempt history without consuming the automatic trigger. No calendar or booking writes."),
        input_schema=_manual_input, output_schema=_manual_output,
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)),
)


def _result(data: dict, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, allow_nan=False))],
        structured_content=data,
        is_error=is_error,
    )


def _error(code: str, message: str) -> CallToolResult:
    return _result({"error": {"code": code, "message": message}}, is_error=True)


def create_server(service: TravelService | None = None, *, itinerary_service=None) -> Server:
    if service is None:
        service = TravelService(create_simulated_coordinator())
    tools = {tool.name: tool for tool in TOOLS}
    validators = {name: Draft202012Validator(tool.input_schema) for name, tool in tools.items()}

    async def list_tools(ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=list(TOOLS))

    async def call_tool(ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult:
        nonlocal itinerary_service
        if params.name not in tools:
            return _error("UNKNOWN_TOOL", "Choose a tool returned by tools/list.")
        arguments = params.arguments if params.arguments is not None else {}
        invalid_message = (
            "Provide a non-empty flight_number and a valid YYYY-MM-DD date."
            if params.name == "get_trip_context" else
            "Provide a valid trip context and omit candidates or supply a non-empty "
            "list of plans with label, local ISO leave_time, summary, and optional string-list history."
        )
        if params.name in ("monitor_trips", "plan_booked_trip"):
            invalid_message = "Provide an empty monitor input or a valid booked-trip selector and optional candidate proposals."
        try:
            # The low-level SDK advertises schemas but leaves argument validation to us.
            if not validators[params.name].is_valid(arguments):
                return _error("INVALID_INPUT", invalid_message)
            if params.name == "get_trip_context":
                data = service.get_trip_context(**arguments)
            elif params.name == "evaluate_trip_plans":
                data = service.evaluate_trip_plans(**arguments)
            else:
                if itinerary_service is None:
                    from travel_agent.composition import create_itinerary_service
                    itinerary_service = create_itinerary_service()
                data = getattr(itinerary_service, params.name)(**arguments)
            return _result(data)
        except (ValueError, InvalidInputError):
            return _error("INVALID_INPUT", invalid_message)
        except Exception:
            # Never return exception text, input values, file paths, or tracebacks.
            return _error("INTERNAL_ERROR", "The travel service could not complete this request.")

    return Server(
        "Travel Agent", version="8.0.0",
        instructions=(
            "Local simulated travel planning. monitor_trips uses the service clock for automatic activation; "
            "plan_booked_trip is for explicit user requests. Existing context/evaluation tools remain available. "
            "The host owns conversation and model inference; Python owns validation and planning decisions."
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
