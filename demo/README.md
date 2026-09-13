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

## LIVE HOST ROADMAP

Current CP6A: recorded, structured host evidence, with no Gmail or model access:

```powershell
.\.venv\Scripts\python.exe -B demo/host_evidence_demo.py
.\.venv\Scripts\python.exe -B demo/host_evidence_demo.py --evidence demo/recorded_host_evidence.json
```

The demo envelope requires `source_type`, `provider`, `flight_number`,
`departure_airport`, `arrival_airport`, `departure_date`, `scheduled_departure`,
and `time_basis`. Optional `scheduled_arrival` and `evidence_timestamp` are retained
as evidence, not planner inputs. All timestamps use the explicitly declared
`America/New_York` basis in this bounded ATL/PHL demo. No personal identifiers,
confirmation number, email body, sender address, or model prose are collected.

The bridge uses production identifier/date/context validators, checks the exact
supported scenario, obtains supplemental context via `get_trip_context`, then
maps evidence fields into `evaluate_trip_plans.context`. Calendar, boarding, gate,
status, delay, and duration assumptions remain visibly `[FIXTURE]`. Booking flight,
route, and departure are `[RECORDED HOST EVIDENCE]`. Unknown fields, including
attempted scores/feasibility/approval overrides, are rejected. Invalid evidence
does not trigger CP5 infrastructure fallback; this rehearsal has no fallback.

Provenance is a demo sidecar, not a production MCP response field or authenticated
provider assertion. `evaluate_trip_plans` validates structure and evaluates
caller-supplied facts; it does not establish booking truth. `plan_booked_trip`
selects existing repository bookings and is deliberately not used to pretend
that evidence has been ingested. No V9 mail extraction/projection is invoked:
those contracts require mail identity, template authority, and reconciliation
evidence that this small host envelope does not claim to supply.

### CP6B: Live Host Evidence Handoff

CP6A remains the Recorded Host Evidence Bridge. CP6B accepts an explicit external
file using exactly the same contract and validators:

```powershell
.\.venv\Scripts\python.exe -B demo/host_evidence_demo.py --evidence <path>
```

For live host-derived data, use `source_type: "LIVE_HOST_EVIDENCE"` and
`provider: "GMAIL_VIA_HOST"`. These are supplied provenance claims, not booking
authentication. Do not relabel recorded evidence as live. Do not commit personal
Gmail evidence files. Tests generate synthetic, non-personal payloads in temporary
files to exercise these labels; they do not access Gmail.

ChatGPT/Gmail extraction occurs outside this repository. The host emits
provider-neutral structured evidence; the local demo validates it before MCP
planning. This web-host demonstration uses an **explicit local file handoff**:
ChatGPT web does not directly attach to the local stdio process. This is a
deployment/transport boundary. A future deployment may use a remotely reachable
MCP transport or another supported host connection without changing deterministic
planning logic.

Booking fields are labeled `[LIVE HOST EVIDENCE]`; supplemental assumptions remain
`[FIXTURE]`. No email body or decision outputs are accepted. Validation failures
stop before MCP starts, without recorded/fixture substitution or infrastructure
fallback. The default command still uses the recorded CP6A payload.

The fixed scenario bounds remain DL1425, ATL to PHL, September 16, 2026 at 19:00,
with `America/New_York` time basis. An optional evidence timestamp cannot be later
than the fixed noon scenario clock. A historical or otherwise incompatible real
flight is rejected; CP6B proves handoff, not arbitrary booking support. The agent
requires neither Gmail credentials nor a specific LLM provider. No network, model,
calendar write, or approval execution is added. The proposed meeting slot remains
demo input; attendee availability and organizer permission are not verified.
