# CMU presentation rehearsal

Run from the repository root with the existing environment. No installation,
provider credentials, external network, or LLM is required.

Preferred rehearsal (attempt MCP; allow infrastructure-only fallback):

```powershell
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py
```

Intentional offline rehearsal:

```powershell
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py --mode offline
```

Strict MCP rehearsal (no fallback):

```powershell
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py --no-fallback
```

Existing CP2 too-late case, now presented through the direct booked-trip service:

```powershell
.\.venv\Scripts\python.exe -B demo/run_cmu_demo.py --mode offline --too-late
```

This intentionally supplies the existing 18:00 / 18:00:00 duplicate proposals.
The real bounded planner returns `NO_FEASIBLE_PLAN`; no recommendation or calendar
adjustment is invented. This is an explicit alternate rehearsal, never automatic
fallback for a failed normal plan.

## What the modes prove

- MCP mode crosses real local stdio, discovers the unchanged four tools, and calls
  `get_trip_context`, `evaluate_trip_plans`, and `plan_booked_trip`. The displayed
  result comes from MCP. The CP2 trace is a separate **local production replay**,
  displayed only after full booked-trip result and governance parity checks. It
  is not a trace returned through MCP. No trace schema extension was made.
- Offline mode executes the existing CP1-CP3 service path and actual CP2 trace.
  It is prominently labeled `OFFLINE FIXTURE FALLBACK`; intentional offline mode
  says MCP was not attempted. It never claims an MCP success.
- Startup/connection failure, missing required tools, transport failure, and
  invalid response structure can enable fallback. Input validation/tool errors,
  legitimate planning failures (including `NO_FEASIBLE_PLAN`), baseline/parity
  mismatches, and changed MCP schemas stop the rehearsal. The CP4 schema digest
  is an acceptance check, not a replacement schema. Unknown errors also stop.
- If the direct path fails, no answer is fabricated. Rehearsal acceptance numbers
  are assertions only; all displayed scores and decisions come from execution.

Everything remains fixture-backed: booked flight, workday calendar, transport,
security/gate timing, fixed September 16 clock, proposals, and permitted meeting
slot. Each invocation uses isolated temporary state. Windows asyncio may use
local loopback internally; this is not an external service dependency.

The meeting adjustment is post-evaluation CP3 demo governance, not a production
MCP tool. It remains `AWAITING_USER_APPROVAL` / `NOT_EXECUTED`. The proposed slot
is demo input; attendee availability and organizer permission are unverified.
This path has no calendar mutation or approval-execution capability. Independent
host connectors are outside that guarantee.

No Gmail, Google Calendar, Graph, OAuth, live host/model integration, or lounge
optimization is included. CP4's standalone host remains strict with no fallback.

## Short presentation sequence

1. Identify the mode, fixed clock, booked trip, and fixture evidence.
2. Show host-style proposals and the deterministic recommendation.
3. Show the actual search stages: initial selection plus two refinement rounds.
4. Explain the Leadership Meeting's soft penalty and proposed adjustment.
5. Stop at `USER APPROVAL REQUIRED` / `CALENDAR WRITE EXECUTED: NO`.
6. Optionally run the too-late command to demonstrate a legitimate refusal.

Focused failure-injection checks (no production sabotage):

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_cmu_rehearsal.py -q
```
