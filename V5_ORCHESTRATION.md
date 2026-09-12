# V5: agent orchestration and main wiring

V5 integrates the V4 deterministic agents into the active flow without changing
the search algorithm, Critic, transport data sources, or model/host architecture.
The V4 tag remains the frozen independent-agent baseline.

## Composition and execution

`travel_agent.composition.create_simulated_coordinator()` constructs FlightAgent,
CalendarAgent, and TransportAgent using the existing simulated tools, plus the
unchanged `BeamSearchPlanner(beam_width=2, depth=3)`. Both demos and the default MCP
server use this factory. TravelService does not construct or orchestrate agents.

```text
TravelService.get_trip_context
  -> Coordinator.get_trip_context
     -> FlightAgent.get_flight(flight_number, date)
     -> CalendarAgent.get_events(date)
     -> explicit calendar priority mapping
     -> TransportAgent.get_estimate()
     -> TripContext

TravelService.evaluate_trip_plans
  -> validate externally supplied context
  -> Coordinator.evaluate_trip_plans
     -> baseline candidates if omitted, otherwise host proposals
     -> validate candidates and discard supplied scores
     -> unchanged Beam Search and Critic
     -> selected plan, finalists, recommendation
```

The standalone `Coordinator.plan_trip` combines context acquisition and evaluation.
Calling evaluation alone does not retrieve new agent data; the service remains
stateless and evaluates the supplied context, as in V2/V3.

## Exact mapping

| Source | TripContext destination | Policy |
|---|---|---|
| FlightAgent's normalized FlightState | `flight` | Use the validated object directly; no repeated flight validation |
| CalendarAgent's chronological events | `calendar_events` | Copy events with explicit `normal -> normal`, `high -> high` mapping |
| TransportEstimate.travel_minutes | `airport_travel_minutes` | Use exactly once; default 45 |
| Existing assumption | `security_minutes` | Keep 20 |
| Existing assumption | `gate_walk_minutes` | Keep 15 |
| Existing assumption | `preferred_buffer_minutes` | Keep 45 |

Unknown calendar priorities raise `InvalidDataError` with agent `calendar`; there
is no case folding, guessed priority, or fallback. CalendarAgent's last-meeting,
conflict, and free-window helpers remain independently available but do not add
new scoring rules. Calendar remains read/reason-only.

Parking (10 minutes) and terminal walking (8 minutes) remain separate in the
agent's TransportEstimate but are **not mapped into TripContext or scored**.
Mode and source are likewise not added to the external context. V1-V4 did not
explicitly model those fields. V5 does not claim the existing gate-walk assumption
fully accounts for them; it preserves the existing scoring model rather than
guessing a new mapping or double-counting components. Changing only parking/walking
does not change scores. Changing travel_minutes does reach TripContext.

## Validation and error ownership

Agents own retrieved-data validation. Coordinator only assembles their outputs
and maps the planner's priority vocabulary. It does not re-parse flight or event
times or call the core context validator on agent-built context.

External context dictionaries are still validated by TravelService, because a
host can supply or modify them. Candidate validation remains before search.

Every TravelAgentError raised by an agent propagates unchanged through Coordinator
and TravelService, including its code, message, agent, object identity, and internal
exception chain. No catch-and-continue or data fallback is added. Later agent calls
and planning do not run after an earlier failure.

MCP retains its V3 public error envelope and sanitization. InvalidInputError maps to
the existing `INVALID_INPUT` response. Other domain failures remain the existing
generic `INTERNAL_ERROR` at the MCP boundary. Their full structured distinctions
remain available inside Coordinator/TravelService; this version does not expose
new error codes or fields over MCP. Raw causes never reach the host.

## Compatibility

TravelService method signatures, JSON keys, and both MCP tool definitions are
unchanged. V4 agents use normalized timestamps with seconds internally. Service
serialization preserves the existing minute-only output for whole-minute values;
nonzero seconds and fractional seconds are retained. Serialization does not mutate
the agent objects or change the represented instants.

The legacy `TravelCoordinator(flight_tool, calendar_tool, planner)` constructor
remains accepted for V1-V4 callers and tests. It wraps the supplied tools in V4
agents and explicitly configures simulated transport at construction. This is
compatibility setup, not a runtime fallback. New construction injects all three
agents explicitly; omitting transport in that form is a configuration error.

## Verification

Run all tests and both demos:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B app.py
.\.venv\Scripts\python.exe -B host_assisted_demo.py
```

The 15 V5 integration tests cover call order, mapping, no repeated agent validation,
priority rejection, component separation, changed simulated travel time, every
agent error category, propagation of real tool failures, no fallbacks, external
validation, wire timestamp compatibility, legacy construction, both baselines,
and unchanged MCP error sanitization.

Expected baselines remain deterministic **16:15 / 76.40**, and host-assisted
**16:20 / 76.80** with alternative **16:10 / 76.00**. Existing MCP stdio tests launch
the actively wired server and verify exactly two tools, both modes, host-score
rejection, and sanitized malformed input.
