# V9 Phase 5A: provider contract and integration boundary review

## Status and authority

Documentation/design only, based on repository HEAD
`b5fcab8036c88dac56045debb984442a6673fa50` on `feature/v9-live-replanning`.
No Python types, serialization, migrations, adapters, authentication, credentials,
MCP, workers or planning behavior change in this slice. No network calls were made.
The recorded baseline is 329 tests + 245 subtests; it was not rerun here.

This review distinguishes existing contracts from proposed normalized schemas.
Proposals below require a later bounded type/test slice before adapter use.
Provider endpoint details, payload mappings, scope strings and account capabilities
are NOT verified by this offline review. Required verification is an open gate,
not a claim that Gmail, Graph, AeroAPI or Routes already satisfies a proposed field.
If a provider cannot supply required evidence, return a structured failure or an
explicit unknown; never invent data to fill a normalized model.

No internal LLM/model SDK. HOST PROPOSES; CORE JUDGES. The host may gather evidence;
Python validates identity, provenance, freshness, permissions and planning policy.
Deterministic mail extraction remains separate from operational flight authority.
V7/V8 remain authoritative and unchanged. No timezone stripping, fuzzy booking
matching, autonomous calendar action, purchase, refund or rebooking is permitted.

## Shared contract rules

Existing `ProviderResult[T]` supplies exactly one normalized value or typed error.
`ProviderError` has a safe category, explicit retryability and optional nonnegative
retry delay. These envelopes are sufficient foundations; extend categories as
specified below before adapters need them. Never pass vendor response objects or
raw exception text into planning, evidence error JSON or host-facing results.

Existing `Provenance` has `source`, aware `observed_at`, and typed `retrieved_by`.
Observation persistence separately accepts aware `retrieved_at`. Proposed provider
read envelopes should carry provider/account scope, provenance and retrieval time
explicitly so calendar and standalone reads have the same audit context. A provider
record/version reference is opaque evidence, not a canonical booking key.

All instant fields must be aware and normalize to UTC. Preserve source IANA zones
separately where local date interpretation matters. Reject naive times, invalid
zones, offset/zone disagreement, and unsupported ambiguous local times. Source
update time, retrieval time, message receipt time and scheduled future travel time
are different concepts. Future schedules are valid; future observations are not.
If source observation time is unavailable, explicitly record a retrieval-time
observation basis; never present it as the provider's update time or make an old
cached observation fresh merely by reading it again. A proposed envelope therefore
needs `observation_time_basis` and nullable `source_updated_at` alongside provenance.

Core freshness uses explicit decision time and policy, currently <=60 minutes
inclusive for live observations. Failure does not erase last-known-good evidence.
Mail receipt age is not operational freshness: retained booking evidence can be old;
sync completion time describes mailbox coverage, not flight currency. Simulated
provenance cannot satisfy live requirements. Null is not zero, false, permission,
or a value copied from an older observation. Adapter mappings report evidence;
core alone judges usability, conflict policy and reliable recommendations.

## Mail: existing coverage and required design

Python needs immutable normalized message evidence and a complete, scoped sync
checkpoint. It does not need vendor mail objects or vendor-specific core columns.

| Concern | Existing contract | Required rule or gap |
|---|---|---|
| Request | `MailSource.sync(since, cursor, page_token)` | Source instance bound to provider/account; aware `since`; null cursor means initial/explicit full recovery; non-null cursor means incremental. Same scope across every page. |
| Identity | `MailMessage(provider, account_id, message_id, version)` | Stable account identity and message identity across supported moves must be reviewed; version is opaque and never used as airline order. |
| Content | Normalized sender, subject, text body, optional HTML, recipients, optional thread | MIME decoding/charset/body-selection rules belong to adapter normalization. Preserve conflicting text/HTML evidence. No remote HTML resources. Define size/part limits and unsupported attachments before implementation. |
| Time | Aware UTC `received_at`, optional provenance | Sent time and provider modification time are absent; propose nullable aware `sent_at` and `source_updated_at` plus read-envelope `retrieved_at`. Document source meaning; do not substitute sent time for receipt. Source offset metadata is optional evidence when needed. |
| Provenance | Required by durable storage despite legacy optional constructor | Provider/account/source and retrieval context must be explicit. Sender matching is not authentication; any authenticated-origin evidence needed by a real template needs a reviewed normalized extension. |
| Page | Tuple of messages, removal IDs, next page token, completed cursor | Application requires exactly one continuation or completion token, including empty pages. Constructor currently only rejects both present; strengthen validation in a later contract slice without breaking retained fake compatibility silently. |
| Removal | Message IDs scoped to source/account | Tombstone is mailbox visibility only. Folder exit/move/deletion must map to a documented sync scope, never travel cancellation. |
| Commit | `MailSynchronization` + `BookingRepository.commit_sync` | Fetch all pages before transaction; persist evidence, projection and cursor atomically. No cursor advancement on incomplete work. |
| Recovery | `CURSOR_EXPIRED`, persistent full-resync flag | Map all documented invalid/expired cursor cases needing reset to this code. Only committed full recovery clears the requirement. |

Mail timestamp extensions cannot simply be added to the existing dataclass:
`_store_messages` compares serialized evidence bytes on replay. A new nullable field
can change those bytes even for old messages. Before type implementation, define
backward-compatible encoding/decoding and replay tests, or a separately versioned
envelope. Do not rewrite immutable extraction/message JSON or silently reprocess it.

Another critical replay gap: provenance inside `MailMessage` is immutable for a
given message/version. Re-fetching identical evidence with a new `observed_at` would
collide. Freeze message evidence provenance semantics and keep per-fetch retrieval
time in a separate envelope/checkpoint. Define a deterministic version mapping for
metadata-only changes versus body changes; a mailbox cursor is not automatically
a per-message version. Do not substitute a content hash without reviewing which
normalized evidence it covers and how corrections/replays are represented.

The current core rejects a message both present and removed in one batch, and
requires consistent terminal tokens. Provider adapters must document how supported
vendor event sequences become an unambiguous batch; if ordering cannot be proved,
fail conservatively. Do not weaken core checks merely to accept raw delta events.

### Provider mapping verification checklist (OPEN; no endpoint claims)

| Provider | Required official-document mapping before implementation |
|---|---|
| Gmail API | Initial message listing/body retrieval and lookback coverage; pagination; transition from full sync to history checkpoint without lost changes; incremental history scope; message ID stability; per-message version strategy; repeated history records; removal/label visibility meaning; cursor expiration and retention; MIME timestamp/provenance mapping; least-privilege read scope and account consent requirements. |
| Microsoft Graph Mail | Initial/delta scope including folders; aggregation of folder checkpoints into an opaque account checkpoint if needed; continuation versus terminal delta token; stable ID behavior across moves; change/version identity; removals versus moves/scope exit; delta reset responses; MIME/body/time mapping; account/tenant capability and least-privilege read scope. |

Exact scope strings and vendor response codes must be captured with official
references in the later authorized verification slice. No scope is selected here.
Opaque tokens must be bound to account, query/lookback and folder scope; never log
them. Token material must not embed access/refresh credentials in ordinary tables.

Mail conclusion: the sync application and evidence/projection transaction are
sound foundations for bounded adapter work. The normalization/version/provenance
and provider mapping gaps above block Gmail implementation. Mail adapters never
choose canonical booking identity, cancellation, schedule authority or eligibility.
Missing coupon/traveler/lifetime proof remains unresolved. A separate later Phase 5
real-mail-template slice must review exact identity, supported booking/change/
cancellation/reinstatement forms, source trust and deterministic fixture coverage.
It must not assume the synthetic Northstar sequence fields exist in real mail.

## Calendar reads: proposed normalized contract

Existing `CalendarProvider.list_events(start, end)` and `get_event(calendar_id,
event_id)` are read-only. `CalendarEvent` already has account/calendar/event/version
identity and aware positive start/end intervals. These are useful but insufficient
for either provider's complete future conflict evidence. `CalendarAuthority` booleans
do not distinguish unknown from false and are not human approval.

Proposed read request: provider/account/calendar scope, aware window start/end,
opaque optional cursor/page token, and explicit recurrence-expansion policy. A full
window query and incremental sync must be distinguishable; a cursor must bind to
its window/query. Provider support for incremental window behavior must be verified.

Proposed timed event record (design only):

- Identity: provider, account ID, calendar ID, event ID, opaque version; nullable
  series ID and original occurrence identity for expanded recurring instances.
- Interval: aware start/end with exclusive end; explicit source start/end zones.
  All-day records use a tagged local-date interval plus explicit calendar zone;
  core converts it deterministically for conflicts. Never invent UTC midnight.
- State: status CONFIRMED/TENTATIVE/CANCELLED/UNKNOWN; normalized busy/free/unknown
  availability; separate deletion tombstone. A tombstone requires identity and
  change evidence, not fabricated start/end values.
- Relationship: organizer identity where disclosed, self-is-organizer and
  self-is-attendee as true/false/unknown, user response ACCEPTED/TENTATIVE/DECLINED/
  NEEDS_ACTION/UNKNOWN. Omitted private details remain null/unknown.
- Audit: provenance, retrieval time, nullable source update time and observation
  time basis. State and response are provider read evidence; core decides whether
  an event blocks travel. A title/description is not authorization.

Proposed page: immutable event and tombstone tuples, next-page token OR completed
sync token, with account/calendar/window scope. Empty delta retains events. Define
atomic snapshot/delta application before persistence; mail sync tables are not a
calendar store. A provider lacking equivalent sync capability returns unsupported
or uses an explicitly reviewed full-read mode, never a fabricated delta token.

Before Google Calendar and Graph Calendar adapters, freeze normalized recurring,
all-day, private, cancelled/deleted and participation semantics; verify each API's
token/version/timezone mapping and read-only scopes. No writes or write scope is
needed. Do not populate `can_reschedule/can_cancel/can_decline` from guessed read
permissions. Preserve the old type for compatibility until a versioned read model
is reviewed. Future write capability evidence, authenticated HITL decision,
conditional execution and verification remain separate Phase 8 requirements.

## Operational flight: proposed normalized contract

Existing `FlightIdentity` binds segment, flight number, route, aware scheduled
departure and origin zone. `FlightObservation` supplies one departure instant,
cancellation, nullable terminal/gate and provenance. It lacks the distinctions
needed for AeroAPI v4; do not overload `departure` with changing time meanings.

Proposed lookup: canonical segment ID, explicit carrier designator and number,
operating date in origin IANA zone, exact origin/destination and known scheduled
departure. Define marketing versus operating carrier association explicitly;
codeshare similarity alone cannot establish identity. Retain a nullable opaque
provider flight reference only after an exact association has been established.

Proposed operational record: associated lookup identity and provider reference;
scheduled/estimated/actual departure and arrival, each explicitly labeled by
milestone (gate/out/in versus runway/off/on); origin/destination source zones;
cancelled and diverted states with unknown representable; nullable actual diverted
destination; nullable departure/arrival terminal and gate; provenance, source update
time where available, retrieval time and observation time basis. Optional estimate,
actual, gate and terminal fields stay null when unavailable. Required association
evidence missing from a response blocks association rather than filling from a
different flight. Never collapse runway and gate times into one unlabeled instant.

Freeze exact association rules before adapter code: require one compatible record
for carrier/number/date/route/scheduled identity, with an explicit reviewed tolerance
policy if schedule changes affect lookup. Zero matches is NOT_FOUND, multiple
unresolved matches AMBIGUOUS_IDENTITY. Never pick the first/closest result or use
fuzzy booking matching. Diversion changes operational evidence, not booked identity.

Operational provider data supplies operational authority subject to core source,
freshness and conflict policy. Conflicting credible departures retain evidence and
use the earlier departure conservatively in later planning. Mail stays booking
evidence. Provider retrieval does not authorize cancellation actions or planning.
Verify actual AeroAPI v4 field availability, account capabilities and milestone
mapping officially before implementing these proposed types/adapters.

## Road traffic: proposed normalized contract

The repository calls this port `TrafficProvider`, not `TransportProvider`.
`TrafficRequest(origin: str, airport: str, departure_time)` and
`TrafficEstimate(request, road_minutes, provenance)` cover a basic road estimate.
An airport code is not an exact road destination, and an untyped origin string
does not establish deterministic routing identity.

Proposed request: explicit typed origin and destination waypoints (validated
coordinates or a reviewed stable location reference), destination airport context,
aware requested departure, road mode DRIVE, traffic preference and requested traffic
model as normalized enums with explicit supported combinations. Origin provenance/
permission is checked upstream; this port does not discover current location.
Define resolution of HOME/WORK and airport dropoff/parking endpoints separately.

Proposed result: exact request association; nonnegative finite traffic-aware road
duration in a fixed unit; nullable static duration and distance with explicit units;
effective traffic model/preference and fallback status; provenance, retrieval time,
observation time basis and nullable source update time. Echo resolved endpoints
where necessary to verify applicability. Missing traffic support must be reported
as unsupported/unavailable or an explicitly labeled fallback that core can reject;
never label a static duration traffic-aware. Zero is valid, missing is not zero.

Freeze departure-time validity, requested/effective model semantics and conversion
rounding before Google Routes implementation; verify supported API combinations
officially. The road provider never includes parking search, parking-to-terminal,
rideshare pickup/fare, security or terminal walking. Phase 6 composes each duration
exactly once and remains responsible for feasibility and departure decisions.

## Host-supplied evidence and credential boundary

Security wait, airport parking, rideshare fare/pickup ETA and current location stay
outside direct Phase 5 adapters. Existing Python-owned closed schemas, numeric/time
validation, scope checks and session-owned location permission references remain.
The host later gathers/submits observations through a reviewed MCP boundary; no
such flow is added here. Source text or a host boolean never grants authority.

Existing `CredentialReference` stores only an environment-variable name, and
`ProviderConfiguration` scopes a provider/account. Future runtime resolves credentials
from environment/configuration or approved secret storage; never hard-code them or
store access/refresh tokens in ordinary evidence tables, fixtures or logs. OAuth
mechanics, refresh and client authentication belong to adapters/runtime. Mail access
is read-oriented for discovery. Select least-privilege scopes after account-specific
official verification; calendar read and future write grants must stay separable.
Read success never establishes permission for a future mutation. No secrets or
example credential values are part of this review.

## Provider-neutral errors

| Condition | Existing/proposed category | Handling rule |
|---|---|---|
| Missing/expired authentication | Existing AUTH_REQUIRED | Surface reauthorization requirement; no blind credential retry. |
| Authorization denied | Existing PERMISSION_DENIED | Preserve scope failure; do not widen permissions automatically. |
| Rate limit | Existing RATE_LIMITED | Retryable only with bounded runtime policy; safe retry delay if supplied. |
| Transient service failure | Existing UNAVAILABLE | Retryability is explicit, not implied by all errors. |
| Timeout | Proposed TIMEOUT | Currently generic sync exception handling maps this to INVALID_RESPONSE. Add typed adapter mapping later; preserve resync requirement. |
| Malformed data | Existing INVALID_RESPONSE | Reject unsafe/incomplete normalization; no guessed placeholder observations. |
| No matching resource | Proposed NOT_FOUND | Not a flight cancellation, mailbox tombstone or empty authoritative snapshot. |
| Multiple uncertain identities | Proposed AMBIGUOUS_IDENTITY | Block association; never choose a candidate heuristically. |
| Invalid/expired recovery cursor | Existing CURSOR_EXPIRED | Persistent full-resync requirement; no generic mapping that loses recovery semantics. |
| Missing provider/account capability | Proposed UNSUPPORTED_CAPABILITY | Fail explicitly; do not silently substitute a different semantic operation. |
| Stale evidence | Existing core DATA_UNAVAILABLE with reason STALE | A core usability result, not a transport error. Preserve evidence and last-known-good data; never refresh timestamps to hide staleness. |

No enum changes are made now. Before extending it, verify stored error decoding,
constructor compatibility and focused tests. Retryable errors do not authorize an
unbounded retry loop or cursor advancement. Adapter errors must be translated once
into this taxonomy; planner code must not inspect raw vendor error formats.

## Integration gates and later slices

| Boundary | Gate and status |
|---|---|
| Before Gmail | OPEN: freeze mail timestamp/version/provenance replay contract; document official pagination/full-to-history checkpoint/version/removal mapping and exact least-privilege OAuth scope. |
| Before Graph Mail | OPEN: document official delta/folder/version/stable-ID/removal mapping, scope-bound composite cursor if required, and exact least-privilege OAuth scope; same mail replay guarantees. |
| Before calendar adapters | OPEN: freeze normalized read schema including recurrence/all-day/tombstones/provenance/sync; document both read scopes and mappings. No write permission required. |
| Before FlightAware | OPEN: freeze exact operational association and milestone mapping; represent missing gate/terminal/estimates conservatively; verify AeroAPI v4 capabilities. |
| Before Google Routes | OPEN: freeze explicit origin/destination/departure contract and traffic-duration/model/fallback semantics; verify API support. |
| Before sustained live mailbox ingestion | OPEN: address or bound all-history reprojection with retained-message/event/segment limits and transaction-duration evidence. Existing 100-page/10,000-item batch limits do not bound retained history. Exceeding bounds must not partially advance cursor. |
| Before another DB migration | OPEN: consolidate migration runner ownership; retain exact migration 1/2 SQL and checksums, schema refusal, rollback, backup and immutable V8 history tests. Current migration-2 use remains through BookingRepository. |
| Before real airline support | OPEN: separately reviewed later Phase 5 deterministic real-mail-template slice; exact stable references, trustworthy ordering and reinstatement evidence, unsupported/unresolved behavior and explicit reprocessing strategy. |
| Before reliable live planning | Phase 6: canonical observation association, trusted source selection/conflicts, freshness, aware timing/nullable-field boundary, active-plan comparison and V7/V8 preservation. |
| Before service/MCP/worker changes | Phase 7: lifecycle/origins/events contracts, tool schema review, claims/leases/recovery and shared service orchestration. |
| Before calendar execution | Phase 8: authenticated human approval, proposal/version/expiry binding, conditional execution, read-back and unknown-outcome reconciliation. |

Migration consolidation is mandatory before a new migration, not an excuse to
change existing schemas in this documentation slice. All-history optimization can
remain deferred for bounded offline work. Manual conflict repair/reissue graphs and
broad template coverage can remain deferred while uncertainty blocks activation.

Recommended Phase 5B: official-document/account-capability verification and a
bounded mail contract freeze, with no Gmail adapter yet. Resolve the mail gaps above,
record exact scopes/mappings with official references, and only then implement
minimal compatible provider-neutral types plus focused replay/contract tests under
explicit authorization. If code changes, run the entire suite. Subsequent separately
reviewed slices can implement Gmail, Graph Mail, deterministic real-mail templates,
Google Calendar reads, Graph Calendar reads, AeroAPI v4 and Routes. Do not infer
authorization for any of these implementations from this review.

## Phase 5A validation

Only this document and the continuation plan change. No contract tests are added
because executable types are unchanged; no HTTP fakes are introduced. Validate the
documentation diff/whitespace, changed-file inventory and empty V8 source/test diff.
No test rerun or fresh passing-baseline claim is made for this documentation slice.
No commit, push, network, authentication or Phase 5B work is performed.
