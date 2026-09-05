# Model-Agnostic Travel Agent — V2

This CMU capstone prototype exposes a model-independent travel-planning core.
It uses only the Python standard library, fake flight/calendar data, and the
existing V1 Beam Search and Critic. No API key, model SDK, or MCP transport is required.

## Two planning modes
- **Deterministic:** omit candidates (or pass `None`) to generate the four V1
  baseline plans from `TripContext` inside the core.
- **Host-assisted:** supply a non-empty list of candidate dictionaries. The
  service validates them and sends them through the same planner and critic.
  Supplied candidates replace the starting baseline; they are not merged with it.

ChatGPT, Claude, Gemini, or another host can own the conversation and inference.
A future host adapter can call the two JSON-compatible operations below. The core
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

## Boundaries and future work

- Beam Search and Critic are unchanged, including duplicate finalists and pruning behavior.
- There is no MCP server, HTTP transport, or actual host connection yet.
- Future MCP/host adapters should wrap `TravelService`; optional standalone model
  adapters should generate candidates outside the core and use environment-based credentials.
- Real flight/calendar integrations, persistence, and timezone handling remain future work.
