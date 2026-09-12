# V8: Itinerary monitoring and proactive trip activation

Baseline: annotated `v7` tag, commit `08dcb2c0be6292c5ca4aa35d94f0663240376abc`.
Branch: `feature/v8-itinerary-monitoring`. V7 planning code is unchanged.

## Ownership and invocation

An external scheduler or host invokes `monitor_trips({})`. MCP is an interface,
not a scheduler. The service resolves its injected clock once, refreshes the
complete itinerary snapshot, applies it transactionally, evaluates each current
segment, and dispatches eligible automatic planning in departure/itinerary/segment
order. Every current segment gets activation evidence, including cancelled and
departed segments. Missing records are retained but excluded from current queries.

An explicit user invokes `plan_booked_trip({"selector": ...})`. The service
refreshes first, resolves exactly one upcoming confirmed segment, and enters the
same planning handoff. Zero matches return `NOT_FOUND`; multiple matches return
`NEEDS_SELECTION` and sorted matches. Supported selector fields are itinerary_id,
segment_id (requires itinerary_id), destination, departure_date. An empty selector
matches all upcoming segments. No language interpretation occurs in Python.

Both paths use `ItineraryService._plan_segment` -> existing
`TravelService.get_trip_context` -> FlightAgent / CalendarAgent / TransportAgent ->
booking/operational identity validation -> existing
`TravelService.evaluate_trip_plans` -> V7 Beam Search / feasibility / Critic.

Source retrieves; ItineraryAgent validates and normalizes; repository maintains
state; ActivationPolicy configures; ActivationEvaluator decides eligibility;
ItineraryService dispatches. The host cannot override validity, identity,
eligibility, provenance, feasibility, or scores. No internal LLM exists.

## Time

Public MCP tools have no `as_of` input. SystemClock uses zoneinfo America/New_York.
FixedClock is injected in tests, including actual stdio subprocess composition.
All source and operational snapshots declare `time_basis: America/New_York`.
Naive/local domain timestamps must include T; offset-bearing, ambiguous DST,
nonexistent DST, and mixed-convention records are rejected. Missing zoneinfo data
fails explicitly; requirements.txt pins tzdata for Windows portability.

One `Clock.now()` call resolves decision time per valid invocation. Refresh,
activation, manual resolution, and provenance share it. Audit started_at and
finished_at use this invocation's logical timestamp; they are not duration
measurements. Interrupted attempts receive the recovery invocation's timestamp.
No private clock is read inside the evaluator or repository.

## Source and identities

The strict fixture source returns an object containing source_id, time_basis,
records. Each itinerary record has itinerary_id and a nonempty segments list.
Each segment requires segment_id, flight_number, departure_date, origin,
destination, scheduled_departure, booking_status. Status is CONFIRMED or CANCELLED.
Source shapes are closed: unrecognized fields and per-record timezone overrides
are rejected. No fallback bookings are invented.

Itinerary identity is (source_id, source_itinerary_id). Segment identity is
(source_id, source_itinerary_id, source_segment_id). Wire identities percent-encode
each component before slash joining. Ordering and schedule are never identities.
Schedule updates preserve IDs; a replacement source segment ID creates new identity.

Identical same-ID records collapse after normalization. Conflicting same-ID
records reject the entire batch. Distinct identities are never semantically merged.
Source order is canonicalized. Each service invocation operates on its configured
source, avoiding use of unrefreshed other-source rows in a shared repository.

## Atomic refresh

The whole snapshot is retrieved, normalized and validated before mutation.
Successful snapshots create/update current state atomically. Content changes
increment itinerary revision; identical content does not. Restoring identical
missing content restores presence without manufacturing a content revision.
Removed segments/itineraries become MISSING_FROM_SOURCE. Cancellation updates
booking status. Both preserve history and prohibit planning.

A valid empty snapshot marks prior source records missing. Invalid snapshots
preserve booking state, audit REJECTED, and start no automatic or manual planning.
Source retrieval failure audits SOURCE_ERROR. No stale-data planning fallback.
Database failures propagate and prevent further dispatch. An unsuccessful SQLite
transaction rolls back, including any audit row in that transaction.

RefreshResult exposes retrieved, created, updated, unchanged, duplicate, invalid,
missing counts; resulting upcoming segments; and sanitized diagnostics. Upcoming
means present + confirmed + scheduled_departure > as_of, not T-24 eligibility.

## Activation

The only production lead-time default is ActivationPolicy.planning_lead_time_minutes
= 1440. The policy is frozen, shared, internally configurable, and accepts only
non-negative integers (not bool). Zero has no predeparture activation window.

ActivationEvaluator receives segment, explicit as_of, policy. It computes:

```
activation_time = scheduled_departure - planning_lead_time
invalid / cancelled               -> INVALID_SEGMENT / CANCELLED
as_of >= scheduled_departure      -> DEPARTED
as_of < activation_time           -> NOT_YET_ELIGIBLE
otherwise                         -> ELIGIBLE
```

Evidence includes exact times, lead minutes, time until departure, reason code,
inclusive lower/exclusive upper bounds, and comparison booleans. Eligibility is
pure and never persisted as a lasting execution state.

## Execution history

Automatic identity: (segment_id, AUTOMATIC_LEAD_TIME). It excludes departure,
revision, lead time, refresh number, and host. Persisted execution states are
NOT_ACTIVATED, PLANNING, COMPLETED, FAILED.

| Eligibility/state | Dispatch |
|---|---|
| Not eligible / any state | NOT_ELIGIBLE |
| Eligible / NOT_ACTIVATED | PLAN |
| Eligible / FAILED | RETRY |
| Eligible / COMPLETED | SUPPRESS_DUPLICATE |

PLANNING is committed before operational lookup. PLAN_FOUND and NO_FEASIBLE_PLAN
both complete the attempt. Lookup, consistency, or planning failures become FAILED.
Each later eligible invocation may make one new numbered attempt; no retry loop
occurs within an invocation. SQLite persistence failure stops dispatch, including
unrelated remaining segments, because durable execution tracking is unavailable.

Completed automatic results suppress future automatic execution even if booking
schedule changes. Return current revision alongside the original completed attempt,
with its original timestamp and revision. Failed attempts can retry against current
valid facts. A replacement segment ID is independently eligible.

Manual USER_REQUEST attempts have independent numbering/history. They do not create,
consume, or update automatic state. A successful manual refresh updates bookings,
but automatic state initialization happens only in monitoring. Automatic completion
never blocks manual replanning. Manual entry bypasses only automatic lead time;
its automatic activation evidence remains truthful.

## Concurrency and interruption

The database-associated nonblocking OS lock is held before automatic refresh and
through dispatch. Windows uses msvcrt byte-range locking; POSIX uses flock. Lock
file existence alone says nothing about ownership. A competing invocation gets
MONITOR_BUSY with no refresh or dispatch. Manual operations share the lock to avoid
replacing the monitor's current booking snapshot; a busy manual result is BUSY.

After acquiring the lock, unfinished automatic attempts are marked FAILED with
INTERRUPTED_EXECUTION. They retry only after successful refresh and current
eligibility. A failed refresh still permits recovery bookkeeping but no planning.
No exactly-once computation is claimed across process crashes. Finished attempts
are durable; an interrupted pure computation can run again.

Manual process termination can leave a manual PLANNING audit row. V8 automatic
recovery intentionally touches only automatic attempts; manual history does not
control future manual requests. There are no autonomous external execution effects.

## SQLite: six tables

| Table | Schema purpose |
|---|---|
| refresh_runs | refresh_id PK; source, decision time, time basis, refresh status, counts JSON, diagnostics JSON |
| itineraries | itinerary_id PK; unique source/source-itinerary pair; current revision, presence, first/last seen, refresh FK |
| itinerary_revisions | itinerary/revision composite PK; immutable normalized snapshot JSON, recorded time, refresh FK |
| segments | segment_id PK; unique itinerary/source-segment pair; booked flight identity, schedule/status/presence; itinerary revision FK |
| automatic_planning_state | segment/trigger composite PK; execution state, last/completed attempt numbers, updated time |
| planning_attempts | attempt_id PK; unique segment/trigger/attempt number; mode, revision, decision/start/finish time, state, activation/provenance/context/result/error JSON |

Foreign keys, status checks, transaction boundaries and deterministic JSON are
enabled. Partial unique indexes permit only one running and one completed
automatic attempt per trigger. Revision triggers prohibit update/delete/replacement of history.
Runtime DB/lock paths are composition configuration, never MCP arguments.

## Operational facts

FixtureFlightTool validates the entire operational fixture and performs exact
(flight_number, departure_date) lookup. A missing record returns unavailable;
conflicting fixture entries fail validation. FlightAgent remains responsible for
operational normalization and identity validation. V8 additionally checks route.
Booking schedule determines activation; operational departure/boarding determine
V7 planning. A different operational clock time on the same booked flight/date is
allowed. No booking timestamp overwrites FlightState.

## MCP

Exactly four tools: get_trip_context, evaluate_trip_plans, monitor_trips,
plan_booked_trip. Both original tools retain their full V7 input/output schemas.
New schemas are defined in travel_agent/adapters/itinerary_schemas.py and tested
against real stdio results. monitor_trips accepts only {}. plan_booked_trip accepts
selector and optional candidate proposals; V7 strips/recomputes candidate extras.

New tools persist local data and are not labeled read-only. Neither is destructive
or open-world. Monitor advertises idempotent automatic dispatch; manual does not.
Refresh audit rows still accumulate on repeated monitoring invocations.
Domain zero-results/failures/selection requests are structured isError=false results.
Malformed calls and unexpected boundary failures use sanitized isError=true errors.
Lazy V8 composition leaves V7-only demos independent of tzdata and MCP packages.

## Run and verify

```
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-mcp.txt
.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.venv\Scripts\python.exe -B app.py
.venv\Scripts\python.exe -B host_assisted_demo.py
.venv\Scripts\python.exe -B -m travel_agent.adapters.mcp_server
```

Default fixtures contain the Sep 11 outbound and Sep 14 return. Actual current
SystemClock time determines normal eligibility; fixtures are not silently redated.
Deterministic examples use FixedClock at the composition/test boundary, never MCP.
Default state lives at .runtime/v8.sqlite3. Tests use disposable local databases.

The test suite covers source/time validation, snapshot rollback, revisions,
missing/cancelled/restored records, eligibility boundaries, persistence, retries,
suppression, replacement/update semantics, locking/interruption, manual independence,
strict operational identity, full V7 regression, and real MCP stdio major flows.

Verification on 2026-09-09: 164 unittest methods (108 retained V7 tests plus
56 V8 tests), zero failures and zero skips. V8 counts: clock 4, agent 8,
repository 6, locking 2, activation 4, execution state 9, operational fixtures 4,
service 12, MCP 7. The execution-state suite includes a real child-process exit
with a committed PLANNING row and OS-lock release, followed by recovery/retry.
Actual stdio covers activation, repeat suppression, five-day early manual planning,
multiple failures/retries, selection, real NO_FEASIBLE_PLAN completion and malformed
override rejection. Tagged V7 input/output schemas compare identical for the old tools.
Demos retain deterministic 16:25 / 77.20 and host-assisted 16:20 / 76.80.

## V7 invariants and V9/V10 limits

V7 gate feasibility, 15-minute policy, Critic arithmetic/weights, score components,
width/depth, parent preservation, deduplication and NO_FEASIBLE_PLAN are unchanged.
No infeasible finalist can be selected. Calendar remains read-only; moving,
rescheduling or cancelling meetings requires explicit approval. No action execution
has been introduced. No internal LLM/model SDK or host-specific decision logic.

V8 retains V7's absence of a current-time lower bound on candidate leave times.
This remains a simulated planner, not an assurance of real-world executability.
Local OS locks are for one machine/local SQLite, not distributed scheduling.

V9 must address timezone-aware multi-airport facts and V7 timestamp migration,
booking/flight changes and distinct change triggers, provider-specific capability
action windows, live operational feeds, continuous replanning and explicit HITL
execution boundaries. ACTION_WINDOW_OPENED, FLIGHT_STATUS_CHANGED, GATE_CHANGED,
TRAFFIC_CHANGED and SECURITY_WAIT_CHANGED must not redefine T-24 activation.

Deferred: Gmail/Outlook, airline APIs, delays/gates, security/traffic/rideshare,
parking/walking, lounge/food/weather, reservations/purchases/booking modification,
notifications, autonomous calendar writes, host SDKs, internal LLMs, LangChain,
LangGraph, CrewAI, vector RAG and observability hardening. V10 remains evaluation,
observability, regression/guardrail/failure evidence and host/model portability.

## Final architectural review

One persistence defect was reproduced: with SQLite's default recursive-trigger
setting, INSERT OR REPLACE could overwrite an existing itinerary revision without
firing the delete guard. A BEFORE INSERT guard now rejects any existing revision
identity, independently of recursive-trigger configuration. The new regression
failed before this change and passed afterward. No V7 production files changed.

Three existing assertions were strengthened: immutability now tests content updates
and deletion with the specific trigger error (rather than a key update which could
fail solely from a foreign key); PLANNING durability is observed through a separate
SQLite connection; duplicate suppression asserts no operational lookup occurs.

Final pytest result: 165 tests and 200 subtests passed, zero failures/skips.
Focused V8 suite: 57 tests. Separate MCP verification: 13 tests and 15 subtests
passed, including actual subprocess stdio. Original 108 tests remain present.
Both V7 demo outputs remain 16:25 / 77.20 and 16:20 / 76.80. Exact old MCP schemas
and V7 service/coordinator/planning/FlightAgent/calendar production paths match v7.

Live temporary-database inspection confirmed exactly six application tables,
foreign_keys=1 on both opened repository connections, integrity_check=ok and no
foreign_key_check violations. Direct inserts proved both automatic partial unique
indexes reject duplicate completed/running attempts. Production SQL uses bound
parameters; fixture/DB paths are composition-owned, not selector-derived.

The sole production wall-clock read is datetime.now(local_zone()) in SystemClock.
No datetime.utcnow or time.time occurs. Activation has no clock/history/MCP access.
Its local-time validation uses the fixed America/New_York convention and installed
timezone rules. There is no internal LLM, scheduling loop, calendar mutation or
external action execution in the new production call paths.

All six known limitations are acceptable within the approved simulated V8 scope:
logical audit timestamps (V10 duration/observability work); interrupted manual
audit rows (V9 lifecycle recovery and V10 evidence); naive single-zone data (V9
timezone-aware migration); local-machine locking (V9 runtime architecture); no
exactly-once crash guarantee (V9 action idempotency/HITL); and inherited V7 absence
of a current-time candidate lower bound (V9 before live execution). None was
expanded or silently fixed during this review.

V8 is ready to freeze within this scope. No commit, merge, tag or push was made.
