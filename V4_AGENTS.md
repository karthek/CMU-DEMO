# V4: independent deterministic agents

V4 adds an agent layer only. The existing application, Coordinator, TravelService,
MCP adapter, planner, and Critic do not import or invoke these agents. V5 will
handle integration. There are no model calls, live APIs, or new dependencies.

## Independent use

```python
from travel_agent.agents.flight_agent import FlightAgent
from travel_agent.agents.calendar_agent import CalendarAgent
from travel_agent.agents.transport_agent import TransportAgent
from travel_agent.tools.flight_tool import FakeFlightTool
from travel_agent.tools.calendar_tool import FakeCalendarTool
from travel_agent.tools.transport_tool import FakeTransportTool

flight = FlightAgent(FakeFlightTool()).get_flight("DL1425", "2026-09-11")
calendar = CalendarAgent(FakeCalendarTool())
events = calendar.get_events("2026-09-11")
last_end = calendar.last_meeting_end(events)
conflicts = calendar.departure_conflicts(events, "2026-09-11T16:45")
windows = calendar.free_windows(events, "2026-09-11T09:00", "2026-09-11T18:00")
transport = TransportAgent(FakeTransportTool()).get_estimate()
```

## Contracts

Each constructor accepts one read-only tool with the retrieval method shown below.
Tools return existing domain dataclasses, not arbitrary dictionaries. Agents
return validated copies; caller-owned and tool-owned objects are never modified.

| Method | Input | Output |
|---|---|---|
| `FlightAgent.get_flight(flight_number, date)` | Non-empty flight number; `YYYY-MM-DD` date | Normalized `FlightState` |
| `CalendarAgent.get_events(date)` | `YYYY-MM-DD` date | Chronologically ordered `list[CalendarEvent]` overlapping that day |
| `CalendarAgent.last_meeting_end(events)` | Event list, normally from `get_events` | Latest end as an ISO string, or `None` for no meetings |
| `CalendarAgent.departure_conflicts(events, departure_time)` | Event list and local ISO departure timestamp | Ordered list of conflicting event copies, or `[]` |
| `CalendarAgent.free_windows(events, window_start, window_end)` | Event list and explicit local ISO search bounds | List of `(start, end)` ISO-string tuples |
| `TransportAgent.get_estimate()` | No request arguments; uses injected simulated tool | Frozen `TransportEstimate` |

Dates are strict `YYYY-MM-DD`. Timestamps use local ISO date/time with `T` and no
timezone offset; normalized outputs include seconds and retain fractional seconds
when supplied. V4 deliberately does not introduce timezone conversion.

FlightAgent trims required text, uppercases the flight number, checks that the
returned flight and departure date match the request, validates boarding <=
departure, and requires a non-negative integer delay. Boarding on the previous
day is allowed. Flight status is preserved; the agent does not invent a new
departure time from a delay or decide that a flight is bookable.

CalendarAgent retrieves through `get_events(date)`, validates every returned event,
then selects events overlapping the requested day. Overnight meetings count when
they overlap that day. It preserves supplied non-empty priority strings exactly;
it does not infer priority. Last-meeting-end is the maximum end, not necessarily
the end of the event with the latest start. Pass the relevant event list to its
reasoning methods; those methods do not retrieve or filter by an implicit date.

Conflict checks use `[start, end)`: departing at the start conflicts; departing at
the end does not. They check the proposed departure instant, not full travel-time
availability. Free windows clip meetings to explicit bounds and merge overlapping
or adjacent busy periods. No events means the entire requested window is free.
No free time means `[]`. Free windows do not imply airport feasibility.

CalendarAgent is **READ + REASON ONLY**. It has no calendar write, cancellation,
or rescheduling method. Future meeting changes require explicit user approval;
V4 neither proposes nor executes such changes.

TransportAgent retrieves through `get_estimate()`. The fake tool returns:

```python
TransportEstimate(
    travel_minutes=45,
    parking_minutes=10,
    terminal_walk_minutes=8,
    mode="drive",
    source="simulated",
)
```

All three durations must be non-negative integers (booleans, floats, strings,
and missing values are rejected). Mode and source must be non-empty strings and
are trimmed. The source is preserved, never relabeled. The fake estimate is a
fixed fixture, not a route or traffic calculation. Components remain separate;
the agent does not aggregate them or change existing TripContext assumptions.

## Failures and integration boundaries

Agents expose the shared hierarchy in `travel_agent/agents/errors.py`:

```text
TravelAgentError
├── InvalidInputError     code: INVALID_INPUT
├── DataUnavailableError  code: DATA_UNAVAILABLE
├── InvalidDataError      code: INVALID_DATA
└── ToolFailureError      code: TOOL_FAILURE
```

Each error has `code`, `message`, `agent`, and `to_dict()`. Agent identifiers are
`flight`, `calendar`, and `transport`. Codes are the machine contract for V5;
message wording is for people and must not be used for branching.

```python
{
    "code": "INVALID_DATA",
    "message": "Boarding time cannot be after departure time.",
    "agent": "flight",
}
```

Invalid request dates, flight numbers, proposed departure times, and free-window
bounds raise `InvalidInputError`. Flight numbers accept a 2-3 character alphanumeric
carrier code, 1-4 flight digits, and an optional letter suffix. Returned flight
fields/times, event data, and transport durations that fail validation raise
`InvalidDataError`. Calendar reasoning methods also classify malformed supplied
event objects as invalid domain data, distinct from invalid query timestamps.

A retrieval tool returning `None` means data is unavailable and raises
`DataUnavailableError`. An empty calendar list is valid: it means no meetings.
Other wrongly shaped data raises `InvalidDataError`. Exceptions raised by retrieval
tools become `ToolFailureError`; the agent stops rather than substituting data.
Process-control exceptions such as `KeyboardInterrupt` propagate untouched.

All agent-generated public messages use fixed wording and trusted field names,
never raw input values or underlying exception messages. Original retrieval and
timestamp parsing exceptions remain internally chained as `__cause__`. Future
boundaries must serialize `to_dict()`, not tracebacks or chained exceptions.
There are no retries, fallbacks, or changes to V3 MCP error handling.

V5 must explicitly map parking and terminal walking into planning without double
counting existing security/gate-walk assumptions. It must also decide how to map
calendar priorities: V4 preserves arbitrary supplied labels, whereas the existing
V2 service accepts only `normal` and `high`. Neither policy is changed in V4.

## Tests

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_flight_agent.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_calendar_agent.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_transport_agent.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_agent_errors.py -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```
