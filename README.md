# Model-Agnostic Travel Agent — V5

V5 wires the independent V4 Flight, Calendar, and Transport agents into the active
Coordinator. Both demos and the MCP server use the shared simulated composition
factory. The service/MCP contracts, Beam Search, Critic, and baseline results are
preserved. See [V5 orchestration](V5_ORCHESTRATION.md) for exact mappings, structured
error propagation, and transport components intentionally excluded from scoring.

This CMU capstone prototype exposes a model-independent travel-planning core.
It uses only the Python standard library, fake flight/calendar data, and the
existing V1 Beam Search and Critic. No API key or model SDK is required.
V3 adds an optional local MCP stdio adapter; standalone operation still requires
only the standard library.

## Two planning modes
- **Deterministic:** omit candidates (or pass `None`) to generate the four V1
  baseline plans from `TripContext` inside the core.
- **Host-assisted:** supply a non-empty list of candidate dictionaries. The
  service validates them and sends them through the same planner and critic.
  Supplied candidates replace the starting baseline; they are not merged with it.

ChatGPT, Claude, Gemini, or another host can own the conversation and inference.
The MCP adapter can call the two JSON-compatible operations below. The core
does not import or identify the host. `MockProvider` remains a compatibility
adapter for V1 callers and delegates to the shared deterministic helpers.

## Run
```bash
python app.py
```

On Windows, activation is optional: use `.venv\Scripts\python.exe -B app.py`.
The demo preserves the V1 recommendation: 16:15, score 76.40, with two duplicate
finalists. Beam width remains 2 and depth remains 3 (two refinement rounds).

## Service boundary

```python
from travel_agent.coordinator import TravelCoordinator
from travel_agent.planning.beam_search import BeamSearchPlanner
from travel_agent.service import TravelService
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.flight_tool import FakeFlightTool

service = TravelService(TravelCoordinator(
    flight_tool=FakeFlightTool(),
    calendar_tool=FakeCalendarTool(),
    planner=BeamSearchPlanner(beam_width=2, depth=3),
))
context = service.get_trip_context("DL1425", "2026-09-11")
baseline_result = service.evaluate_trip_plans(context)
host_result = service.evaluate_trip_plans(context, candidates=[{
    "label": "Host buffer",
    "leave_time": "2026-09-11T16:20",
    "summary": "An exploratory option from the host.",
    "history": ["Host suggestion"],
}])
```

`get_trip_context` returns a dictionary containing `flight`, `calendar_events`,
and travel durations. `evaluate_trip_plans` returns `mode`, `selected_plan`,
`finalists`, and a deterministic `recommendation`. A host may write its own
explanation using the structured result.

Candidates require non-empty `label`, `leave_time`, and `summary` strings.
`history` is an optional list of strings, defaulting to an empty list. Times must
be local ISO date/time strings without timezone offsets, matching V1. Caller
scores and unrelated candidate fields are discarded; the Critic owns scoring.
An empty candidate list is an error, not a request for deterministic mode.
Malformed input raises `ValueError` before search. Inputs are copied, not mutated.

The service is stateless: the evaluation call accepts caller-supplied context.
Validation checks its structure and basic consistency, not whether its facts
are authoritative. Feasibility remains governed by the existing Critic, including
its limitations. No booking or calendar changes occur.

Run a simulated host flow (no model/network call):

```bash
python host_assisted_demo.py
```

The flow is: host retrieves context → host proposes candidates → service validates
→ Beam Search refines → Critic scores → structured results return to the host.
In deterministic mode, the core supplies the initial candidates instead.

## Run tests
```bash
python -B -m unittest discover -s tests -v
```

Tests cover the original planner, V1 output compatibility, both service modes,
invalid inputs, input immutability, and operation with model/provider imports blocked.

## V3: local MCP server

Install the optional official Python MCP SDK into the project environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
```

The adapter pins `mcp==2.2.0`, verified against the current official SDK v2
documentation on September 7, 2026. It uses the documented low-level `Server`
API to control `isError` and `structuredContent`, including malformed-argument
errors. JSON Schema validation uses `jsonschema`, a dependency of the MCP SDK.
No MCP packages are imported by the core or either standalone demo.

Official references:
- [Python SDK overview](https://py.sdk.modelcontextprotocol.io/)
- [Low-level Server and structured results](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/)
- [SDK client and stdio transport](https://py.sdk.modelcontextprotocol.io/client/transports/)

Launch from this repository directory:

```powershell
.\.venv\Scripts\python.exe -B -m travel_agent.adapters.mcp_server
```

The process waits for MCP messages on stdin; it is not an interactive CLI.
Stdout is reserved for protocol messages, and logging goes to stderr. A local
MCP client normally launches and stops this process itself. No port is opened.

### What a host discovers

The server identifies itself as `Travel Agent`, version `3.0.0`. It advertises
exactly two read-only tools with semantic descriptions and JSON input/output schemas:

| Tool | Required arguments | Optional arguments | Successful structured result |
|---|---|---|---|
| `get_trip_context` | `flight_number`, `date` | None | The V2 context dictionary |
| `evaluate_trip_plans` | `context` | `candidates` (omitted/null selects baseline) | The V2 planning result |

The descriptions explain simulated data, how to chain the calls, both planning
modes, and that the Critic computes every score. Host-provided scores are ignored
by the unchanged V2 service. Candidate generation and evaluation do not call a model.

Responses contain the same JSON in MCP `structuredContent` and a text content
block so clients can consume structured data or show it to their model.
Invalid tool arguments return `isError: true` with this shape:

```json
{
  "error": {
    "code": "INVALID_INPUT",
    "message": "Provide a non-empty flight_number and a valid YYYY-MM-DD date."
  }
}
```

Unknown tool names use `UNKNOWN_TOOL`; unexpected service failures use
`INTERNAL_ERROR`. Messages are fixed and sanitized: they contain no exception
text, stack traces, paths, or echoed input values. Error results are not successful
planning outputs. Invalid JSON-RPC envelopes are handled by the SDK's protocol
error mechanism, before travel tool dispatch.

### Local host launch configuration

For a host supporting the `mcpServers` configuration format, this is a template.
Replace both absolute paths with your project location; each host's setup UI or
configuration format must be checked before use. `PYTHONPATH` lets module launch
work even when the host starts in a different working directory.

```json
{
  "mcpServers": {
    "travel-agent": {
      "command": "C:/path/to/model_agnostic_travel_agent_v1/.venv/Scripts/python.exe",
      "args": ["-B", "-m", "travel_agent.adapters.mcp_server"],
      "env": {
        "PYTHONPATH": "C:/path/to/model_agnostic_travel_agent_v1"
      }
    }
  }
}
```

No host configuration is installed automatically. A host must support launching
local stdio MCP servers; support is specific to the host product and edition.
Remote-only hosts cannot connect to this local process directly.

### MCP verification

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_mcp_server.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

The MCP test client launches the actual server as a subprocess and connects over
stdio. It tests discovery, context retrieval, omitted/null candidates, host
candidates with forged scores, malformed calls, and recovery after errors.
In-memory MCP tests additionally inject an unexpected service failure and verify
sanitization. An optional-dependency test runs both demos with Python site-packages
disabled. MCP tests skip when the optional SDK is absent; the original tests remain
runnable without installing it.

## Boundaries and future work

- Beam Search and Critic are unchanged, including duplicate finalists and pruning behavior.
- V3 provides local MCP stdio only: no HTTP transport or real AI host is configured yet.
- MCP/host adapters wrap `TravelService`; optional standalone model
  adapters should generate candidates outside the core and use environment-based credentials.
- Real flight/calendar integrations, persistence, and timezone handling remain future work.
