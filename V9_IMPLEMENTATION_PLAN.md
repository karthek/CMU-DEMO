# V9 implementation plan and continuation record

## Baseline and Phase 2 slice (historical)

Frozen V8: `fe802c26e430e9cde97d87dba9f94b0e52d8aa3a`, annotated `v8`.
Branch: `feature/v9-live-replanning`. Initial clean baseline verification:
165 pytest tests and 200 subtests passed, no failures/skips.

This document precedes foundation implementation. This session implements only
aware-time primitives, provider-neutral contracts/configuration, host observation
contracts, freshness/provenance policy, and pure material-change evaluation/tests.
No adapters, network calls, database writes/migrations, MCP additions, worker,
calendar execution or new planning behavior are enabled in this slice.
Existing four tools and all V7/V8 tests remain unchanged.

## Scope and invariants

V9 = live data + continuous replanning + HITL calendar actions. No internal LLM or
model SDK. HOST PROPOSES; CORE JUDGES. AGENT PREPARES; HUMAN COMMITS. Uncertainty
blocks reliable new recommendations rather than authorizing additional actions.
The host owns language, presentation, notifications and approval UI; Python owns
validation, provenance, freshness, conflict resolution, policies, evaluation,
permission checks, persistent state and verified execution. MCP is not a scheduler.

Keep V8 T-24 automatic activation and independent early manual planning. Preserve
automatic history under (segment_id, AUTOMATIC_LEAD_TIME); new material-change
triggers must have distinct identities and not reset V8 completion state.

## Inspected architecture and migration conflicts

V8 ItineraryService refreshes a complete fixture snapshot, resolves one injected
naive NY clock value, stores through ItineraryRepository, evaluates activation,
then calls a shared _plan_segment -> TravelService -> Coordinator -> FlightAgent /
CalendarAgent / TransportAgent -> BeamSearchPlanner.search_outcome -> feasibility /
Critic. MCP exposes get_trip_context, evaluate_trip_plans, monitor_trips,
plan_booked_trip. Keep these paths runnable throughout incremental V9 work.

Explicit conflicts to resolve in later migration phases:

1. V8 contracts reject aware times and serialize naive historical JSON. New aware
   primitives cannot simply be passed into V8; never strip timezone information.
2. V7 timing uses only road + security + gate walking. V9 needs mode-specific
   pickup/parking/transfer/dropoff components and a current-decision-time lower bound.
3. V8 FlightState requires text gate and has no terminal; live gate/terminal are
   nullable. Do not manufacture them or change V8's contract silently.
4. V8 source refresh is a complete snapshot; Gmail history/Graph delta are patches.
   An adapter synchronization store must assemble current bookings; an empty delta
   must never mark all bookings missing.
5. V8 source-scoped IDs cannot alone reconcile duplicate mailboxes. Keep aliases
   and provenance separately from canonical booking/segment identity.
6. V8 holds a local OS lock over planning, and recovers all unfinished automatic
   work after lock acquisition. Avoid long network calls under this lock; later
   claim/version operations need explicit leases and recovery tests. Local SQLite
   is not distributed execution coordination.
7. V8 completed attempts suppress further automatic planning, including updated
   schedules. V9 change runs must preserve those historical attempts.
8. V8 caller-supplied contexts are simulated facts, not authenticated live facts.
   Reliable V9 service planning must assemble validated stored observations itself.

## Components and provider contracts

Provider Protocols expose normalized dataclasses only; adapters translate vendor
payloads/errors. Inject credentials, HTTP clients, clocks, configuration and fakes.
No credential values in repr, fixtures, SQLite application records or Git. Phase 2
configuration stores environment-variable references, not resolved secrets.

| Interface | Later concrete adapters | Responsibility |
|---|---|---|
| MailSource | GmailMailSource, OutlookMailSource (Graph) | Normalized messages and opaque incremental cursors |
| ItineraryExtractor | Deterministic airline templates | Sender/domain, subject, structured text/HTML, regex and airport/flight/time validation |
| CalendarProvider | GoogleCalendarProvider, OutlookCalendarProvider (Graph) | Normalized events/permissions/version; conditional execution and read-back |
| FlightProvider | FlightAwareAeroApiProvider | Flight operational observations, nullable terminal/gate |
| TrafficProvider | GoogleRoutesProvider | Road estimates for requested future departure and current near-departure estimates |

Gmail and Outlook may both be connected; Google and Outlook calendars may both be
connected. Initial mail lookback defaults to configurable 365 days. Persist a
provider/account cursor only after all pages/extraction/state application succeeds.
Use Gmail history and Graph delta subsequently. Expired cursors require an explicit
resync, not a fabricated empty snapshot. Watch/event hints may enqueue refreshes;
daily synchronization remains the guaranteed fallback.

Extraction yields RESOLVED or UNRESOLVED with evidence and missing/conflicting
fields. UNRESOLVED cannot activate. Initial templates must be bounded and tested
against booking/change/cancellation variants. No heuristic completion of dates,
zones, traveler identity or flight numbers. Provider failures use typed safe error
codes (auth, permission, rate limit, unavailable, invalid response, cursor expired).

Cross-mailbox reconciliation proposal: canonical booking identity uses exact
normalized issuing carrier + confirmation reference + configured traveler identity;
segment identity uses provider-issued stable segment/ticket-coupon identity where
available. Maintain account/message aliases and message versions independently.
Without these identifiers, exact flight/date/route equality alone is insufficient
to merge distinct bookings: retain UNRESOLVED reconciliation evidence. Schedule
changes preserve canonical IDs only when linked by exact booking/segment identity.
Cancellation never means automatic refund/rebooking. No fuzzy LLM deduplication.

## Aware time and database migration plan

New timestamps must be aware; normalize comparison/arithmetic to UTC. Preserve
airport/source IANA timezone metadata separately. Local-to-instant conversion
requires an explicit zone; reject nonexistent local times; ambiguous local times
require explicit fold evidence. Offset-bearing source time must agree with declared
IANA zone when both are present. Never default to America/New_York for V9 data.

Phase 2 includes only a pure V8 local-time conversion helper requiring the legacy
time_basis argument. It rejects ambiguity even with inferred fold. It writes nothing.

Later use explicit schema_migrations(version, checksum, applied_at), a transaction,
foreign_keys=ON, backup/rollback tests and schema precondition checks. A V8 database
has no version table; recognize its exact six-table schema as legacy, not an empty DB.
Never UPDATE/REPLACE immutable revisions or rewrite planning_attempt JSON. Preserve
the six V8 tables and add normalized V9 current-time projections referencing legacy
IDs/revisions. Conversion uses stored refresh time_basis only for known V8 records.
Unconvertible/missing-zone rows become UNRESOLVED, never guessed. Re-running the
migration must be idempotent and preserve old JSON byte-for-byte.

Proposed additional tables, to justify and finalize with migration tests:
- schema_migrations: explicit schema history.
- provider_sync_state and mail_messages: provider/account cursors and versioned
  normalized messages needed for idempotent incremental extraction.
- booking_aliases: deterministic cross-mailbox canonical identity links.
- live_observations: normalized typed payload, source, timestamps, credibility and
  provenance; retrieval failures stored separately from last-known-good values.
- retrieval_attempts: failure/success evidence without invalidating fresh data.
- traveler_origins: HOME/WORK and scoped per-trip overrides.
- travel_state: lifecycle, current aware projection, active plan/snapshot version
  and next due refresh; immutable old attempts remain referenced.
- host_events: durable events with idempotency/deduplication keys and delivery cursor.
- action_proposals and action_executions: authority/version/expiration, human
  decision evidence, execution claims, provider idempotency and verification.
Use normalized tables, not vendor-specific application tables. Physical observation
tables versus a discriminated live_observations table is a migration-phase decision;
the four host table names are stable logical contracts either way.

## Observations, freshness and host input

No direct security, parking, rideshare or current-location adapters. The host submits
predefined security_observations, parking_observations, rideshare_observations,
location_observations. Typed rows bind to a trip/segment (location may be unscoped),
record aware observed_at, source and retrieved_by=HOST. Parking categories are
ECONOMY, DAILY, TERMINAL/GARAGE only. Location requires explicit permission and
precision evidence. Source text is evidence, not an authorization grant.

Schema validation rejects unknown authoritative fields, bool-as-number, nonfinite
or negative durations/prices, invalid coordinates and naive/future observation
times. Applicability checks match segment and airport. Permission metadata is a
claim to validate against session authority later, never sufficient write approval.

Universal configurable freshness default: 60 minutes, inclusive. Age > 60 is
DATA_UNAVAILABLE. Keep retrieval attempt failure separate: last-known-good <=60
remains usable. Do not replace good observations with failed retrieval placeholders.
Host/provider/simulated provenance is explicit; simulated rows cannot satisfy live
requirements. Numeric zero is valid, missing is not zero.

HOST_INPUT_REQUIRED reports segment, required/optional logical tables, reason and
freshness requirement. Ask only for required missing/stale rows; optional rows
remain optional. Submission validation/freshness is not a planning decision.
Preserve credible conflicting source values/timestamps; no averaging. Credible
flight conflicts use the earlier departure until resolved and emit DATA_CONFLICT.
Credibility is core-assessed from source policy, not a host boolean.

## Planning and transport migration (later)

Evaluate HOME and WORK, each with DRIVE_AND_PARK and RIDESHARE when supported by
fresh required inputs. A trip override does not overwrite HOME/WORK defaults.
Normal result returns at most best Home and best Work; expose all branches only on
request. Drive components: road + parking + parking-to-terminal + security + gate
movement. Rideshare: pickup + road + terminal dropoff + security + gate movement.
Each duration appears once. Cost cannot override feasibility, then time/reliability,
then convenience, then cost. No price-only replanning. Gate movement within the same
terminal has a configurable 5-minute V9 policy assumption, not universal evidence.
New candidates must satisfy leave_time >= decision_time; en-route plans continue
toward the airport, never turn around or delay arrival because of a flight delay.
V7 weights/results remain a compatibility baseline until this explicit migration.

## ReplanningEvaluator foundation and subsequent integration

Pure evaluator compares a current validated snapshot with the latest ACTIVE PLAN
snapshot, never the previous polling snapshot. Deltas accumulate relative to that
plan baseline; update it only when a reliable revised plan becomes active. Inputs
are core-built validated snapshots, not exposed directly as host MCP decisions.

| Change | Default decision |
|---|---|
| Road deterioration >=10 min | Replan |
| Rideshare pickup + road deterioration >=10 min | Replan |
| Security increase >=10 min | Replan |
| Absolute flight departure change >=15 min | Replan |
| New cancellation | Immediate replan + flight-cancelled event |
| Known terminal changes | Always replan |
| Same known terminal, gate changes | Informational only |
| New calendar conflict in active travel window | Replan |
| Selected parking unavailable | Replan |
| Price/fare alone | No replan |
| Required DATA_UNAVAILABLE recovers | Re-evaluate |

Missing required input blocks creation/revision of a reliable recommendation even
when triggers exist. Return reasons and missing inputs, retain the prior plan as
historical. On recovery, rerun; publish PLAN_CHANGED only when recommendation
materially differs. Retrieval failure alone with fresh good data is not a trigger.
Nullable terminal/gate disappearance is uncertainty, not an invented terminal
change. Required movement data policy must decide whether reliability is blocked.
En-route delay produces information and a CONTINUE_TO_AIRPORT constraint; other
material changes may still replan under that constraint. Never mechanically shift
leave time by the delay. Cancellation never authorizes ticket execution.

Phase 2 returns typed decisions/evidence only, not events, plans or side effects.

## Monitoring, lifecycle and host events (later)

Keep replaceable run-once worker around the SAME application methods used by MCP.
Due-work policy owns configurable daily mail sync, 4-hour operational refresh in
T-24 to T-2 before recommended leave, 30-minute refresh from that boundary, and
30-minute road traffic while EN_ROUTE_TO_AIRPORT. Use fresh authorized current
location if present; location absence cannot block the whole system. Stop road
traffic after ARRIVED_AT_AIRPORT; flight/terminal/gate continue until departure.
Define PLANNING, READY_TO_DEPART, EN_ROUTE_TO_AIRPORT, ARRIVED_AT_AIRPORT,
AIRPORT_MONITORING, FLIGHT_DEPARTED. State transitions require deterministic evidence,
not host-authored permission claims. No embedded cron/APScheduler/cloud dependency.

Persist PLAN_CHANGED, FLIGHT_DELAYED, FLIGHT_CANCELLED, TERMINAL_CHANGED,
CALENDAR_CONFLICT, APPROVAL_REQUIRED, DATA_UNAVAILABLE and DATA_CONFLICT. Discovery
and unchanged routine refreshes are silent. Deduplicate by segment + event type +
material plan/state version. Python stores events; host polls/acknowledges and owns
presentation/notifications. No direct email/SMS/push delivery.

## Calendar HITL (later, no writes in Phase 2)

Detect conflict -> inspect actual organizer/provider permissions -> prepare exact
mutation -> persist server proposal -> APPROVAL_REQUIRED -> explicit human decision
through host -> recheck authority/event version/expiration -> claim execution once
-> execute -> read back -> verify -> replan.

Support reschedule/cancel only when authority permits; attendee decline only with
provider permission. Proposal contains provider account/calendar/event identity,
expected version, immutable intended mutation, expiry and one-use nonce. A boolean
approved=true is never enough. Authenticated approval must reference proposal ID
and challenge and bind to the authorized human/session; host semantic text is not
evidence. Trust transport/host approval attestations explicitly; an untrusted host
cannot cryptographically prove human intent merely by copying an ID. Final approval
transport/authentication design is a mandatory review gate before execution code.
Timeout/unknown execution outcome requires reconciliation/read-back, not blind
retry. Do not hold SQLite transactions across provider I/O or claim distributed
exactly-once effects. No autonomous meeting changes, flight purchases or refunds.

## MCP evolution (review before changing contracts)

The four V8 tools remain unchanged in Phase 2. Later extend application services,
not expose provider classes. Plan a small state/events query capability, typed host
observation submission capability, and proposal-decision capability. Existing
monitor_trips/plan_booked_trip should share live orchestration where compatible.
Finalize exact tool count and versioned aware input/output schemas at integration
review; do not add placeholder tools now. Never expose vendor objects or caller
overrides for scores, eligibility, replanning, current time or authorization.

## Phases and tests

0. Verify V8 tag, clean tree, 165 tests; branch from exact V8. DONE.
1. Write this durable plan before code. DONE.
2. Foundation only: aware primitives/conversion, typed provider/config contracts,
   host observation validation/freshness, ReplanningPolicy/Evaluator and tests.
   Run full suite; stop for review. DONE, committed as caa600c96e27f1e4dc33711ef2592cd549e3d5d3.
3. Explicit DB migrations, observations/sync storage and deterministic mail
   extraction/reconciliation with offline fixtures. CURRENT REVIEW SLICE.
   Origin/lifecycle state persistence is deferred until its service contracts exist.
4. Concrete Gmail/Graph/Google Calendar/FlightAware/Routes adapters after official
   API documentation/account capability verification; injected HTTP stubs, no live
   network in tests. Persist cursors only after committed processing.
5. Aware V9 planning/transport/feasibility migration and reliable-data gating;
   conflict conservatism; compare active-plan snapshots; retain V7 compatibility.
6. Shared live monitoring service, events/host-input protocol, MCP schema review,
   replaceable worker, local claim/recovery integration and real stdio tests.
7. HITL authentication design review, proposal storage, conditional execution,
   verification/reconciliation and failure/duplicate-execution tests.

Tests: naive rejection, DST folds/gaps, UTC-equivalent instants, explicit legacy
zone conversion, provenance/applicability/permission checks, age 60 vs >60, future
observations, fresh good data after failed retrieval, unavailable/recovery,
9/10 minute deterioration and 14/15 schedule change, negative improvements,
terminal/gate nullability, price-only silence, plan-relative cumulative changes,
en-route delays, cancellation, deterministic output, no network and no V8 changes.
Later test migration rollback/idempotency/history preservation, provider cursors,
reconciliation collisions, permission changes, duplicate execution and read-back
uncertainty, worker crash recovery, exact MCP contracts and real subprocess stdio.

## Non-goals

No internal LLM/SDK, host-specific reasoning, consumer UI, direct notification
service, hotel/rental-car/rail discovery, off-airport parking, autonomous flight
booking/rebooking/refund, observability/LangSmith, LLM-as-judge, or V10 host
portability evaluation. No direct security/parking/rideshare/location integrations.

## Phase 2 completion (historical)

Completed 2026-09-10. New files:
- travel_agent/live/time.py: explicit aware/UTC primitives, IANA local conversion,
  DST fold/gap validation and pure legacy conversion (no writes).
- travel_agent/live/observations.py: four closed host-row parsers/typed dataclasses,
  provenance, inclusive freshness decisions, fresh-data host request suppression,
  applicability and session-owned location-permission reference checking.
- travel_agent/live/providers.py: typed Mail/Calendar-read/Flight/Traffic Protocols,
  normalized data, nullable flight gate/terminal, structured error envelopes.
- travel_agent/live/config.py: provider/account configuration, environment variable
  references and configurable synchronization defaults; no credential loading.
- travel_agent/live/replanning.py: typed snapshots/policies/results and pure
  material-change evaluation; reliable-plan blocking, recovery and en-route guard.
- travel_agent/live/__init__.py and four tests/test_v9_*.py modules.

Verification: 30 foundation tests and 22 subtests passed. Full suite: 195 tests,
222 subtests passed, no failures/skips (all 165 existing tests retained). V8 service,
planner, itinerary package, composition and MCP adapter/schema diff against v8 is
empty. The four existing MCP tools remain the only exposed tools. All provider
contract tests use in-test deterministic fakes, no network. No schema migration,
live provider adapter, worker, calendar write port, conflict resolver, host-event
store or service integration is implemented yet. same_terminal_gate_buffer_minutes
is configuration only until the explicit V9 feasibility migration.

Next instruction should review this slice, then authorize the next bounded phase.
Before implementing adapters, verify official provider documentation and required
account/scopes; this foundation intentionally assumes no endpoint details. Before
database writes, finalize schema projections/versioning and test byte-preserved V8
history. Before HITL writes, settle authenticated human approval evidence; proposal
IDs or host booleans alone cannot establish human intent. No commits/pushes/tags
were made in this slice, and v8 remains frozen.

## Phase 3 implementation / resume here

Implemented from `caa600c96e27f1e4dc33711ef2592cd549e3d5d3` on
`feature/v9-live-replanning`. Before implementation the complete Phase 2 baseline
passed: 195 tests and 222 subtests. This phase is left uncommitted for review.

### Persistence and migration entry point

`travel_agent.live.repository.LiveRepository(path, as_of=aware_time)` explicitly
opts into the V9 schema. The existing V8 repository, service, planner, composition
and four MCP tools are unchanged. V8 initialization continues creating exactly
its six-table schema; opening through LiveRepository migrates that database.
New LiveRepository databases contain both the six legacy tables and the V9 tables.
No real user database was migrated during development; tests use temporary files.

Migration version 1 is additive and transactional (`BEGIN IMMEDIATE`). It compares
the complete SQLite schema, including indexes and immutable-revision triggers,
against the known V8 schema or current V9 schema. Partial, altered and unknown
schemas are refused. The version/checksum ledger is checked on every V9 open;
unknown versions or changed checksums are refused. Foreign keys are enabled and
checked before commit. Repeated migration does not change the ledger timestamp.
Pending caller transactions are rejected. Migration never updates legacy rows,
rewrites JSON, or converts legacy timestamps. A failed DDL step rolls back all
new tables and the ledger. Optional `backup_path` uses SQLite backup before the
migration and refuses to overwrite an existing file. A backup is a pre-migration
snapshot; callers must coordinate access if they need it to match concurrent writes.

Actual new tables (no vendor-specific tables):

| Table | Identity and contents |
|---|---|
| schema_migrations | Integer migration version, SHA-256 SQL checksum, aware applied_at |
| provider_sync_state | PK provider/account; opaque cursor; last successful and attempted timestamps; SUCCESS/ERROR and typed error JSON |
| mail_messages | PK provider/account/message/version; normalized message JSON, extraction JSON, received_at and stored_at |
| live_observations | Immutable observation ID; discriminator; segment association; typed normalized payload JSON; source, observed_at, retrieved_at, retrieved_by |
| retrieval_attempts | Immutable attempt ID; provider/account; type/segment/time; FK to successful observation OR typed failure JSON |

Observation types are SECURITY, PARKING, RIDESHARE, LOCATION, FLIGHT and TRAFFIC.
Provenance is retained in normalized payloads as well as queryable columns.
Location can be unscoped. Other observations require a segment identity. The
optional `legacy_segment_id` is an enforced FK to V8 `segments`, with an equality
constraint to the logical segment ID; its itinerary can be obtained through V8's
existing FK. New logical segment identities do not yet have a canonical V9 table
and therefore are not given a fictitious foreign key. The later projection phase
must establish that association before using evidence for reliable planning.
Storage of a location permission reference does not grant permission: callers
still validate observations against session authority through Phase 2 contracts.

Mail, observations and retrieval attempts reject updates, deletes and SQL REPLACE
of existing identities. Identical repository-level replays are no-ops; identity
reuse with different evidence fails. Observation failures have separate rows and
never replace last-known-good observations or change their freshness timestamps.
The repository returns observation records with canonical JSON; it does not yet
select a trusted/latest observation or resolve live-data source conflicts.

### Mail model and deterministic extraction

`live.mail.MailMessage` is the single normalized contract, re-exported from
`live.providers` for Phase 2 compatibility. Existing positional arguments remain
valid. Added fields are provider, optional thread ID, recipient tuple and typed
provenance. Legacy default UNSPECIFIED provider/missing provenance remains usable
for old fake contract tests but is refused for durable mail and supported extraction.
Timestamps normalize to UTC; source metadata is preserved. The HTML body is retained
as evidence, with a local HTMLParser producing extraction text. Text whitespace,
line endings, entities and block boundaries normalize deterministically; script
and style contents are excluded from extraction. No remote content is fetched.

`ItineraryExtractor` selects exactly one injected rule by explicit matching.
Each rule validates sender/template, reads fields, checks internal consistency,
and returns BOOKING, CHANGE, CANCELLATION, UNRESOLVED or NOT_TRAVEL. Results carry
the full source message/provenance and rule identity. Multiple matching rules are
UNRESOLVED. Suspicious flight/booking messages without a supported rule are
UNRESOLVED; clearly unrelated messages are NOT_TRAVEL.

The sole default rule is `synthetic-northstar/v1`, for the fictional Northstar Air
and the reserved `northstar.example.test` sender domain. It uses exact subjects
and labeled single-segment fields. It is a framework proof, not real airline email
support or a general prose parser. Its bounded airport registry is ATL/PHL/LAX.
Required extraction identity: carrier, flight number, origin, destination and
explicit departure date, plus event consistent with the subject. Optional times,
arrival and booking/traveler/segment references are never invented. A supplied
timestamp requires its explicit airport IANA zone and matching numeric offset;
invalid dates, DST gaps, inconsistent date/offset/route, and conflicting duplicate
fields across text/HTML alternatives are UNRESOLVED. Ambiguous DST folds are
resolved only by the explicit supplied offset. Missing time remains null.

Seven synthetic local fixtures cover booking, change, cancellation, unrelated,
incomplete, conflicting and HTML-only messages. Tests derive further malformed,
multipart, DST, replay and cross-provider variants. No LLM, SDK, live API, web
scraping, credentials or external airline samples are used.

### Reconciliation and synchronization boundary

`reconcile` operates on immutable extraction evidence, including records reloaded
from SQLite. Canonical segment-group identity is the exact tuple of validated
carrier + booking reference + configured traveler reference + stable segment/coupon
reference, encoded unambiguously and SHA-256 hashed. Caller-owned traveler
configuration must match; mailbox recipient text alone never authorizes identity.
Missing references or message/version collisions become unresolved evidence.
Flight/date/route similarity alone never merges bookings. Distinct booking,
traveler or segment references stay separate even when flight details are identical.

Equivalent information across provider messages contributes to one group while
retaining every source/version. Identical message replay contributes only once.
Changes/cancellations retain stable identity and all evidence. If event types or
segment facts differ, `needs_resolution=True`: no current revision is selected
using receipt order, and no activatable V8 segment is written. Booking-reference
reuse across booking lifetimes is likewise conservatively unresolved if facts
differ. Authoritative change ordering and canonical current-state projection are
deliberately deferred. Rule/version changes require explicit future reprocessing;
they cannot silently overwrite stored extraction results.

`MailSource` and `MailSyncPage` remain provider-neutral delta ports. Gmail history,
Graph delta and page tokens stay opaque to extraction/reconciliation. The caller
must assemble all pages before `complete_sync`; the repository atomically commits
their messages/extraction results and completed cursor, checking expected prior
cursor, provider/account scope and time order. An extraction failure or message
collision rolls back the entire batch and cursor. Empty deltas retain old evidence.
Failed sync preserves the cursor and last successful time while recording a safe
typed error. CURSOR_EXPIRED is evidence for future explicit resync, not an empty
mailbox. No pagination runner or tombstone application is implemented here;
mail deletion must never be interpreted as flight cancellation.

### Limits and exact next boundary

Reconciliation is pure and reconstructible from persisted extraction evidence;
no canonical itinerary/alias projection is persisted yet. Neither resolved nor
unresolved mail is wired into V8 activation, snapshot application or planning.
The schema has no origin/lifecycle, host-event or HITL/action tables. There are no
live adapters, network calls, worker, MCP changes, calendar writes or V10 additions.

Recommended next bounded phase: offline synchronization application and canonical
booking projection. Define/test complete page and removal handling, explicit
resync, authoritative change ordering, canonical aliases and aware current-state
projections while retaining all V8 history and automatic attempts. Review those
contracts before wiring a shared application service. Live adapter implementation
remains a separate later phase requiring official API/account capability checks.

Final Phase 3 verification: 236 tests and 245 subtests passed in 26.91 seconds,
with no failures or skips. This adds 41 tests and 23 subtests to the unchanged
195-test/222-subtest baseline. Coverage includes migration idempotency, V8 history
and completed-attempt preservation, optional backup, DDL failure rollback, unknown
schema/version refusal, foreign keys, cursor failure/replay, immutable evidence,
all six observation types, extraction states, multipart conflicts, DST and
cross-provider reconciliation. Existing tests were not changed or removed.
The tracked diff and new source files were reviewed; no V8 source file changed.
No commit, staging, push or later-phase implementation was performed.
